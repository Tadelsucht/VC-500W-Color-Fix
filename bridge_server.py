#!/usr/bin/env python3
#
# Bridge Server for Brother VC-500W Web Interface
# Receives HTTP requests and forwards them to the printer via TCP
#

import http.server
import socketserver
import json
import tempfile
import os
import io
from urllib.parse import urlparse, parse_qs
import logging
from email.message import EmailMessage
from email import policy

# Import the existing labelprinter modules
from labelprinter.connection import Connection
from labelprinter.printer import LabelPrinter

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def parse_multipart_form_data(content_type, body):
    """Parse multipart form data without using deprecated cgi module"""
    # Extract boundary from content type
    if 'boundary=' not in content_type:
        raise ValueError('No boundary found in content type')
    
    boundary = content_type.split('boundary=')[1].strip()
    if boundary.startswith('"') and boundary.endswith('"'):
        boundary = boundary[1:-1]
    
    # Parse the multipart data
    parts = body.split(f'--{boundary}'.encode())
    form_data = {}
    files = {}
    
    for part in parts[1:-1]:  # Skip first empty part and last closing part
        if not part.strip():
            continue
            
        # Split headers and content
        if b'\r\n\r\n' in part:
            headers_data, content = part.split(b'\r\n\r\n', 1)
        else:
            continue
            
        headers_text = headers_data.decode('utf-8', errors='ignore')
        
        # Parse Content-Disposition header
        name = None
        filename = None
        for line in headers_text.split('\r\n'):
            if line.lower().startswith('content-disposition:'):
                # Extract name and filename
                if 'name="' in line:
                    start = line.find('name="') + 6
                    end = line.find('"', start)
                    name = line[start:end]
                if 'filename="' in line:
                    start = line.find('filename="') + 10
                    end = line.find('"', start)
                    filename = line[start:end]
        
        if name:
            # Remove trailing \r\n
            content = content.rstrip(b'\r\n')
            
            if filename:
                # This is a file field
                files[name] = {
                    'filename': filename,
                    'content': content
                }
            else:
                # This is a regular field
                form_data[name] = content.decode('utf-8', errors='ignore')
    
    return form_data, files

class BridgeHandler(http.server.BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        """Handle CORS preflight requests"""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        """Handle GET requests"""
        parsed_path = urlparse(self.path)
        
        if parsed_path.path == '/status':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            
            response = {
                'status': 'running',
                'message': 'Bridge server is running and ready to accept print jobs'
            }
            self.wfile.write(json.dumps(response).encode())
            
        elif parsed_path.path == '/printer-status':
            self.handle_printer_status_request()
            
        else:
            self.send_response(404)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(b'Not Found')

    def do_POST(self):
        """Handle POST requests"""
        if self.path == '/print':
            self.handle_print_request()
        else:
            self.send_response(404)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(b'Not Found')

    def handle_printer_status_request(self):
        """Handle printer status requests"""
        try:
            # Get printer IP from query parameters
            parsed_path = urlparse(self.path)
            query_params = parse_qs(parsed_path.query)
            printer_ip = query_params.get('ip', ['192.168.178.120'])[0]
            
            logger.info(f"Printer status request for IP: {printer_ip}")
            
            # Connect to the printer and get status
            connection = Connection(printer_ip, 9100)
            printer = LabelPrinter(connection)
            
            # Get configuration and status
            config = printer.get_configuration()
            status = printer.get_status()
            
            connection.close()
            
            # Calculate paper/tape information
            tape_info = {
                'present': False,
                'type_mm': 0,
                'total_mm': 0,
                'remaining_mm': 0,
                'percentage': 0
            }
            
            if config.tape_width and config.tape_length_initial and status.tape_length_remaining != -1.0:
                tape_info['present'] = True
                tape_info['type_mm'] = int(config.tape_width * 25.4)  # Convert inches to mm
                tape_info['total_mm'] = int(config.tape_length_initial * 25.4)  # Convert inches to mm
                tape_info['remaining_mm'] = int(status.tape_length_remaining * 25.4)  # Convert inches to mm
                tape_info['percentage'] = int((tape_info['remaining_mm'] / tape_info['total_mm']) * 100) if tape_info['total_mm'] > 0 else 0
            
            # Send success response
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            
            response = {
                'success': True,
                'printer': {
                    'model': config.model,
                    'serial': config.serial,
                    'state': status.print_state,
                    'job_stage': status.print_job_stage,
                    'job_error': status.print_job_error
                },
                'tape': tape_info
            }
            self.wfile.write(json.dumps(response).encode())
            
        except Exception as e:
            logger.error(f"Printer status error: {str(e)}")
            
            # Send error response
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            
            response = {
                'success': False,
                'error': str(e),
                'tape': {
                    'present': False,
                    'type_mm': 0,
                    'total_mm': 0,
                    'remaining_mm': 0,
                    'percentage': 0
                }
            }
            self.wfile.write(json.dumps(response).encode())

    def handle_print_request(self):
        """Handle print requests from the web interface"""
        try:
            # Get content type and length
            content_type = self.headers.get('content-type', '')
            content_length = int(self.headers.get('content-length', 0))
            
            if not content_type.startswith('multipart/form-data'):
                raise ValueError('Expected multipart/form-data')
            
            # Read the request body
            body = self.rfile.read(content_length)
            
            # Parse the multipart form data
            form_data, files = parse_multipart_form_data(content_type, body)
            
            # Extract parameters
            printer_ip = form_data.get('printer_ip', '192.168.178.120')
            mode = form_data.get('mode', 'vivid')
            cut = form_data.get('cut', 'full')
            
            # Get the image file
            if 'image' not in files:
                raise ValueError('No image file provided')
            
            image_data = files['image']['content']
            image_filename = files['image']['filename']
            
            logger.info(f"Print request received - IP: {printer_ip}, Mode: {mode}, Cut: {cut}, File: {image_filename}")
            
            # Save the uploaded image to a temporary file
            with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as temp_file:
                temp_file.write(image_data)
                temp_filename = temp_file.name
            
            try:
                # Connect to the printer and send the print job
                connection = Connection(printer_ip, 9100)
                printer = LabelPrinter(connection)
                
                # Get printer status to verify connection
                status = printer.get_status()
                logger.info(f"Printer status: {status.print_state}")
                
                # Print the image
                with open(temp_filename, 'rb') as image_file:
                    print_result = printer.print_jpeg(image_file, mode, cut)
                
                connection.close()
                logger.info("Print job sent successfully")
                
                # Send success response
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                
                response = {
                    'success': True,
                    'message': 'Print job sent successfully',
                    'printer_status': status.print_state
                }
                self.wfile.write(json.dumps(response).encode())
                
            finally:
                # Clean up temporary file
                try:
                    os.unlink(temp_filename)
                except:
                    pass
                    
        except Exception as e:
            logger.error(f"Print error: {str(e)}")
            
            # Send error response
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            
            response = {
                'success': False,
                'error': str(e)
            }
            self.wfile.write(json.dumps(response).encode())

    def log_message(self, format, *args):
        """Override log message to use our logger"""
        logger.info(f"{self.client_address[0]} - {format % args}")

def run_server(port=8080):
    """Run the bridge server"""
    handler = BridgeHandler
    
    try:
        with socketserver.TCPServer(("", port), handler) as httpd:
            logger.info(f"Bridge server started on port {port}")
            logger.info(f"Open web_interface.html in your browser to use the printer interface")
            logger.info("Press Ctrl+C to stop the server")
            httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Server error: {str(e)}")

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Brother VC-500W Bridge Server')
    parser.add_argument('-p', '--port', type=int, default=8080, 
                       help='Port to run the bridge server on (default: 8080)')
    
    args = parser.parse_args()
    
    print("Brother VC-500W Bridge Server")
    print("=============================")
    print(f"Starting server on port {args.port}...")
    print("This server bridges web requests to the printer via TCP")
    print()
    
    run_server(args.port) 