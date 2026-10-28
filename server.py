#!/usr/bin/env python3
import datetime
import json
import re
import subprocess
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HOST = "0.0.0.0"
PORT = 10071
PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_PROTOCOL_VERSIONS = {"2025-11-25", "2026-07-28"}
SERVER_INFO = {"name": "columbia-mcp-server", "version": "1.0.0"}
TOOLS = [
    {"name": "today", "description": "Return today's date in the requested Python strftime format.",
     "inputSchema": {"type": "object", "properties": {"format": {"type": "string", "description": "Python strftime format, e.g. %Y-%m-%d", "default": "%Y-%m-%d"}}, "additionalProperties": False}},
    {"name": "weekday", "description": "Return the weekday for a date.",
     "inputSchema": {"type": "object", "properties": {"date": {"type": "string", "description": "Date in YYYY-MM-DD format"}}, "required": ["date"], "additionalProperties": False}},
    {"name": "timer", "description": "Send an update every ten seconds and an alarm when the requested number of seconds has elapsed.",
     "inputSchema": {"type": "object", "properties": {"alarm": {"type": "integer", "minimum": 1, "description": "Seconds until the alarm"}}, "required": ["alarm"], "additionalProperties": False}},
    {"name": "calculator", "description": "Evaluate a mathematical expression using the bc calculator.",
     "inputSchema": {"type": "object", "properties": {"expression": {"type": "string", "description": "Expression accepted by bc, e.g. (2+3)*4"}}, "required": ["expression"], "additionalProperties": False}},
]
TOOL_NAMES = {tool["name"] for tool in TOOLS}

def rpc_error(request_id, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": err}

def rpc_result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}

def text_result(text):
    return {"content": [{"type": "text", "text": str(text)}]}

def call_tool(name, args):
    if not isinstance(args, dict):
        raise ValueError("Tool arguments must be a JSON object.")
    if name == "today":
        fmt = args.get("format", "%Y-%m-%d")
        if not isinstance(fmt, str):
            raise ValueError("'format' must be a string.")
        try:
            result = datetime.datetime.now().strftime(fmt)
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid strftime format: " + str(exc))
        return text_result(result)
    if name == "weekday":
        value = args.get("date")
        if not isinstance(value, str):
            raise ValueError("'date' is required and must be YYYY-MM-DD.")
        try:
            date = datetime.date.fromisoformat(value)
        except ValueError:
            raise ValueError("Invalid date. Use YYYY-MM-DD.")
        return text_result(date.strftime("%A"))
    if name == "timer":
        alarm = args.get("alarm")
        if isinstance(alarm, bool) or not isinstance(alarm, int) or alarm < 1 or alarm > 86400:
            raise ValueError("'alarm' must be an integer from 1 to 86400.")
        return {"_timer_alarm": alarm}
    if name == "calculator":
        expression = args.get("expression")
        if not isinstance(expression, str) or not expression.strip():
            raise ValueError("'expression' is required and must be a non-empty string.")
        if not re.fullmatch(r"[0-9a-zA-Z_+*/%().,\s^<>!=&|?:-]+", expression):
            raise ValueError("Expression contains unsupported characters.")
        try:
            proc = subprocess.run(["bc", "-l"], input=expression + "\nquit\n", text=True,
                                  capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("Could not run bc.")
        if proc.returncode != 0 or proc.stderr.strip():
            raise ValueError("Invalid calculator expression.")
        output = proc.stdout.strip()
        if not output:
            raise ValueError("No result was produced.")
        return text_result(output.splitlines()[-1])
    raise KeyError(name)

class MCPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ColumbiaMCP/1.0"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def send_json(self, status, obj, extra_headers=None):
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("MCP-Protocol-Version", getattr(self, "response_protocol_version", PROTOCOL_VERSION))
        self.send_header("Cache-Control", "no-cache")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlparse(self.path).path != "/mcp":
            self.send_json(404, rpc_error(None, -32601, "Endpoint not found"))
            return
        self.send_json(405, {"jsonrpc": "2.0", "error": {"code": -32600, "message": "GET SSE stream is not supported; send MCP requests using POST."}, "id": None}, {"Allow": "POST"})

    def do_DELETE(self):
        self.send_json(405, {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Session termination is not supported."}, "id": None}, {"Allow": "POST, GET"})

    def do_POST(self):
        if urlparse(self.path).path != "/mcp":
            self.send_json(404, rpc_error(None, -32601, "Endpoint not found"))
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                self.send_json(400, rpc_error(None, -32600, "Invalid or missing Content-Length"))
                return
            raw = self.rfile.read(length)
            request = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(400, rpc_error(None, -32700, "Parse error"))
            return
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            self.send_json(400, rpc_error(request.get("id") if isinstance(request, dict) else None, -32600, "Invalid Request"))
            return
        # Default to the assignment protocol; negotiate a supported version per request.
        header_version = self.headers.get("MCP-Protocol-Version")
        self.response_protocol_version = header_version if header_version in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        method = request["method"]
        request_id = request.get("id")
        is_notification = "id" not in request

        if method == "initialize":
            if is_notification:
                self.send_response(202); self.send_header("Content-Length", "0"); self.end_headers(); return
            params = request.get("params") or {}
            requested_version = params.get("protocolVersion", PROTOCOL_VERSION) if isinstance(params, dict) else PROTOCOL_VERSION
            version = requested_version if requested_version in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
            self.response_protocol_version = version
            result = {"protocolVersion": version,
                      "capabilities": {"tools": {}},
                      "serverInfo": SERVER_INFO,
                      "instructions": "Use tools/list to discover available tools. The MCP endpoint is /mcp."}
            self.send_json(200, rpc_result(request_id, result), {"MCP-Session-Id": str(uuid.uuid4())})
            return
        if method == "notifications/initialized" or method.startswith("notifications/"):
            self.send_response(202); self.send_header("Content-Length", "0"); self.end_headers(); return
        if method == "ping":
            if is_notification:
                self.send_response(202); self.send_header("Content-Length", "0"); self.end_headers(); return
            self.send_json(200, rpc_result(request_id, {})); return
        if method == "tools/list":
            if is_notification:
                self.send_response(202); self.send_header("Content-Length", "0"); self.end_headers(); return
            self.send_json(200, rpc_result(request_id, {"tools": TOOLS})); return
        if method == "tools/call":
            params = request.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                self.send_json(200, rpc_error(request_id, -32602, "Invalid params")); return
            name = params["name"]
            if name not in TOOL_NAMES:
                self.send_json(404, rpc_error(request_id, -32601, "Tool not found: " + name)); return
            args = params.get("arguments", {})
            try:
                result = call_tool(name, args)
            except ValueError as exc:
                self.send_json(200, rpc_error(request_id, -32602, str(exc))); return
            except RuntimeError as exc:
                self.send_json(200, rpc_error(request_id, -32603, str(exc))); return
            except Exception as exc:
                self.send_json(200, rpc_error(request_id, -32603, "Internal error: " + str(exc))); return
            if is_notification:
                self.send_response(202); self.send_header("Content-Length", "0"); self.end_headers(); return
            if isinstance(result, dict) and "_timer_alarm" in result:
                self.stream_timer(request_id, result["_timer_alarm"], params.get("_meta", {}))
                return
            self.send_json(200, rpc_result(request_id, result)); return
        if is_notification:
            self.send_response(202); self.send_header("Content-Length", "0"); self.end_headers(); return
        self.send_json(200, rpc_error(request_id, -32601, "Method not found: " + method))

    def stream_timer(self, request_id, alarm, meta):
        if request_id is None:
            self.send_response(202); self.send_header("Content-Length", "0"); self.end_headers(); return
        progress_token = meta.get("progressToken") if isinstance(meta, dict) else None
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("MCP-Protocol-Version", getattr(self, "response_protocol_version", PROTOCOL_VERSION))
        self.send_header("Connection", "close")
        self.end_headers()
        start = time.monotonic()
        next_update = 10
        try:
            while True:
                elapsed = time.monotonic() - start
                remaining = alarm - elapsed
                if remaining <= 0:
                    message = rpc_result(request_id, text_result("ALARM: timer finished after %d seconds." % alarm))
                    self._write_sse(message)
                    break
                if elapsed >= next_update:
                    if progress_token is not None:
                        message = {"jsonrpc": "2.0", "method": "notifications/progress",
                                   "params": {"progressToken": progress_token,
                                              "progress": min(int(elapsed), alarm), "total": alarm,
                                              "message": "Timer update: %d seconds elapsed, %d seconds remaining." % (int(elapsed), max(0, int(remaining)))}}
                    else:
                        message = {"jsonrpc": "2.0", "method": "notifications/message",
                                   "params": {"level": "info", "logger": "timer",
                                              "data": "Timer update: %d seconds elapsed, %d seconds remaining." % (int(elapsed), max(0, int(remaining)))}}
                    self._write_sse(message)
                    next_update += 10
                time.sleep(min(0.25, max(0.01, remaining)))
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.close_connection = True

    def _write_sse(self, message):
        data = json.dumps(message, separators=(",", ":"))
        self.wfile.write(("event: message\ndata: " + data + "\n\n").encode("utf-8"))
        self.wfile.flush()

if __name__ == "__main__":
    print("Starting MCP server on http://0.0.0.0:%d/mcp" % PORT, flush=True)
    ThreadingHTTPServer((HOST, PORT), MCPHandler).serve_forever()
