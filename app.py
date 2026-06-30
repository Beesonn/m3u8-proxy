import re
import json
import urllib.parse
from urllib.parse import urlparse, urljoin
from typing import Optional, Dict, List
from quart import Quart, request, Response, stream_with_context
import aiohttp
from aiohttp import ClientTimeout, TCPConnector
import asyncio

app = Quart(__name__)

connector = None
session = None

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 Firefox/137.0",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.5",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
}

class DomainGroup:
    def __init__(self, patterns: List[str], origin: str, referer: str, custom_headers: Optional[Dict[str, str]] = None):
        self.patterns = [re.compile(p, re.IGNORECASE) for p in patterns]
        self.origin = origin
        self.referer = referer
        self.custom_headers = custom_headers or {}

DOMAIN_GROUPS = [
    DomainGroup(
        patterns=[
            r"(?i)\.buzz$",
            r"(?i)\.buzz/",
            r"(?i)\.click$",
            r"(?i)\.click/",
            r"(?i)cinewave2\.site",
            r"(?i)\.cinewave2\.site$",
            r"(?i)streamzone1\.site",
            r"(?i)\.streamzone1\.site$",
        ],
        origin="https://megaplay.buzz",
        referer="https://megaplay.buzz/",
        custom_headers={"Cache-Control": "no-cache", "Pragma": "no-cache"}
    ),
]

@app.before_serving
async def create_session():
    global connector, session
    connector = TCPConnector(limit=100, limit_per_host=20, ssl=False)
    session = aiohttp.ClientSession(connector=connector, timeout=ClientTimeout(total=60))

@app.after_serving
async def close_session():
    global connector, session
    if session:
        await session.close()
    if connector:
        await connector.close()

def validate_url(url: str):
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return url
    return None

def get_url(line: str, base: str):
    try:
        return urljoin(base, line)
    except Exception:
        return base

def generate_headers(url: str, custom_origin: Optional[str] = None, custom_referer: Optional[str] = None):
    headers = DEFAULT_HEADERS.copy()
    
    if custom_origin and custom_referer:
        headers["Origin"] = custom_origin
        headers["Referer"] = custom_referer
    elif custom_origin:
        headers["Origin"] = custom_origin
        referer = custom_origin if custom_origin.endswith('/') else f"{custom_origin}/"
        headers["Referer"] = referer
    else:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        
        for group in DOMAIN_GROUPS:
            if any(pattern.search(hostname) for pattern in group.patterns):
                headers["Origin"] = group.origin
                headers["Referer"] = group.referer
                headers.update(group.custom_headers)
                break
        else:
            if parsed.scheme and parsed.hostname:
                origin = f"{parsed.scheme}://{parsed.hostname}"
                headers["Origin"] = origin
                headers["Referer"] = f"{origin}/"
    
    return headers

def process_m3u8_line(line: str, scrape_url: str, origin_param: Optional[str], referer_param: Optional[str]):
    if not line:
        return ""
    
    if line.startswith('#'):
        if line.startswith("#EXT-X-KEY") and "URI=\"" in line:
            uri_start = line.find("URI=\"") + 5
            uri_end = line.find('"', uri_start)
            if uri_start > 0 and uri_end > uri_start:
                key_uri = line[uri_start:uri_end]
                resolved = get_url(key_uri, scrape_url)
                encoded = urllib.parse.quote(resolved, safe='')
                new_q = f"url={encoded}"
                if origin_param:
                    new_q += f"&origin={urllib.parse.quote(origin_param)}"
                if referer_param:
                    new_q += f"&referer={urllib.parse.quote(referer_param)}"
                return line[:uri_start] + f"/?{new_q}" + line[uri_end:]
            return line
        
        if line.startswith("#EXT-X-MAP:URI=\""):
            inner_url = line[16:-1]
            resolved = get_url(inner_url, scrape_url)
            encoded = urllib.parse.quote(resolved, safe='')
            new_q = f"url={encoded}"
            if origin_param:
                new_q += f"&origin={urllib.parse.quote(origin_param)}"
            if referer_param:
                new_q += f"&referer={urllib.parse.quote(referer_param)}"
            return f'#EXT-X-MAP:URI="/?{new_q}"'
        
        if "URI=" in line or "URL=" in line:
            if ':' in line:
                colon_pos = line.find(':')
                prefix = line[:colon_pos + 1]
                attrs = line[colon_pos + 1:]
                
                result_parts = [prefix]
                first = True
                for attr in attrs.split(','):
                    if not first:
                        result_parts.append(',')
                    first = False
                    
                    if '=' in attr:
                        eq_pos = attr.find('=')
                        key = attr[:eq_pos].strip()
                        value = attr[eq_pos + 1:].strip().strip('"')
                        
                        if key in ("URI", "URL"):
                            resolved = get_url(value, scrape_url)
                            encoded = urllib.parse.quote(resolved, safe='')
                            new_q = f"url={encoded}"
                            if origin_param:
                                new_q += f"&origin={urllib.parse.quote(origin_param)}"
                            if referer_param:
                                new_q += f"&referer={urllib.parse.quote(referer_param)}"
                            result_parts.append(f'{key}="/?{new_q}"')
                        else:
                            result_parts.append(attr)
                    else:
                        result_parts.append(attr)
                return ''.join(result_parts)
            return line
        
        return line
    
    resolved = get_url(line, scrape_url)
    encoded = urllib.parse.quote(resolved, safe='')
    new_q = f"url={encoded}"
    if origin_param:
        new_q += f"&origin={urllib.parse.quote(origin_param)}"
    if referer_param:
        new_q += f"&referer={urllib.parse.quote(referer_param)}"
    return f"/?{new_q}"

@app.route('/', methods=['GET', 'HEAD', 'OPTIONS', 'POST'])
async def proxy():
    if request.method == 'OPTIONS':
        response = Response("")
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, HEAD"
        response.headers["Access-Control-Allow-Headers"] = "*"
        response.headers["Access-Control-Expose-Headers"] = "*"
        response.headers["Access-Control-Max-Age"] = "86400"
        response.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
        response.headers["Vary"] = "Origin"
        return response
    
    target_url = request.args.get('url')
    if not target_url:
        return Response("Missing URL", status=400)
    
    target_url = validate_url(target_url)
    if not target_url:
        return Response(f"Invalid URL: {target_url}", status=400)
    
    origin_param = request.args.get('origin')
    referer_param = request.args.get('referer')
    custom_headers_json = request.args.get('headers')
    
    headers = generate_headers(target_url, origin_param, referer_param)
    
    for header_name in ['Range', 'If-Range', 'If-None-Match', 'If-Modified-Since']:
        if header_name in request.headers:
            headers[header_name] = request.headers[header_name]
    
    if custom_headers_json:
        try:
            custom_headers = json.loads(custom_headers_json)
            for k, v in custom_headers.items():
                headers[k] = v
        except json.JSONDecodeError:
            pass
    
    try:
        method = request.method
        print(f"GET: {target_url}")
        
        async with session.request(
            method=method,
            url=target_url,
            headers=headers,
            allow_redirects=True
        ) as resp:
            
            print(f"STATUS: {resp.status}")
            
            content_type = resp.headers.get('Content-Type', '').lower()
            
            is_m3u8 = (
                'mpegurl' in content_type or
                'application/vnd.apple.mpegurl' in content_type or
                'application/x-mpegurl' in content_type or
                target_url.lower().endswith('.m3u8')
            )
            
            if is_m3u8:
                content = await resp.text()
                if content.strip().startswith('#EXTM3U'):
                    lines = content.splitlines()
                    processed_lines = []
                    for line in lines:
                        processed_lines.append(process_m3u8_line(line, target_url, origin_param, referer_param))
                    response_text = '\n'.join(processed_lines)
                    response = Response(response_text)
                    response.headers["Content-Type"] = "application/vnd.apple.mpegurl"
                    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                else:
                    response = Response(content, status=resp.status)
                    for header in ['Content-Type', 'Cache-Control', 'Expires', 'Last-Modified', 'ETag']:
                        if header in resp.headers:
                            response.headers[header] = resp.headers[header]
                
                response.headers["Access-Control-Allow-Origin"] = "*"
                response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, HEAD"
                response.headers["Access-Control-Allow-Headers"] = "*"
                response.headers["Access-Control-Expose-Headers"] = "*"
                return response
            
            if request.method == 'HEAD':
                response = Response("", status=resp.status)
                for header in ['Content-Type', 'Content-Length', 'Content-Range', 
                             'Accept-Ranges', 'Cache-Control', 'Expires', 
                             'Last-Modified', 'ETag', 'Content-Encoding', 'Vary']:
                    if header in resp.headers:
                        response.headers[header] = resp.headers[header]
                
                response.headers["Access-Control-Allow-Origin"] = "*"
                response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, HEAD"
                response.headers["Access-Control-Allow-Headers"] = "*"
                response.headers["Access-Control-Expose-Headers"] = "*"
                if 'Accept-Ranges' not in response.headers:
                    response.headers["Accept-Ranges"] = "bytes"
                return response
            
            data = await resp.read()
            response = Response(data, status=resp.status)
            
            for header, value in resp.headers.items():
                if header.lower() not in ['content-encoding', 'transfer-encoding', 'connection']:
                    response.headers[header] = value
            
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, HEAD"
            response.headers["Access-Control-Allow-Headers"] = "*"
            response.headers["Access-Control-Expose-Headers"] = "Content-Length, Content-Range, Accept-Ranges, Content-Type, Cache-Control, Expires, Vary, ETag, Last-Modified"
            
            return response
        
    except Exception as e:
        print(f"ERROR: {target_url} - {e}")
        return Response(f"Failed to fetch target URL: {str(e)}", status=500)

@app.errorhandler(404)
async def not_found(e):
    return Response("Not Found", status=404)

@app.errorhandler(500)
async def internal_error(e):
    return Response("Internal Server Error", status=500)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
