#!/usr/bin/env python
#
# Copyright 2018 Espressif Systems (Shanghai) PTE LTD
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Utility for testing the web server. Test cases:
# Assume the device supports 'n' simultaneous open sockets
#
# HTTP Server Tests
#
# 0. Firmware Settings:
# - Create a dormant thread whose sole job is to call httpd_stop() when instructed
# - Measure the following before httpd_start() is called:
#     - current free memory
#     - current free sockets
# - Measure the same whenever httpd_stop is called
# - Register maximum possible URI handlers: should be successful
# - Register one more URI handler: should fail
# - Deregister on URI handler: should be successful
# - Register on more URI handler: should succeed
# - Register separate handlers for /hello, /hello/type_html. Also
#   ensure that /hello/type_html is registered BEFORE  /hello. (tests
#   that largest matching URI is picked properly)
# - Create URI handler /adder. Make sure it uses a custom free_ctx
#   structure to free it up

# 1. Using Standard Python HTTP Client
# - simple GET on /hello (returns Hello World. Ensures that basic
#   firmware tests are complete, or returns error)
# - POST on /hello (should fail)
# - PUT on /hello (should fail)
# - simple POST on /echo (returns whatever the POST data)
# - simple PUT on /echo (returns whatever the PUT data)
# - GET on /echo (should fail)
# - simple GET on /hello/type_html (returns Content type as text/html)
# - simple GET on /hello/status_500 (returns HTTP status 500)
# - simple GET on /false_uri (returns HTTP status 404)
# - largest matching URI handler is picked is already verified because
#   of /hello and /hello/type_html tests
#
#
# 2. Session Tests
# - Sessions + Pipelining basics:
#    - Create max supported sessions
#    - On session i,
#          - send 3 back-to-back POST requests with data i on /adder
#          - read back 3 responses. They should be i, 2i and 3i
#    - Tests that
#          - pipelining works
#          - per-session context is maintained for all supported
#            sessions
#    - Close all sessions
#
# - Cleanup leftover data: Tests that the web server properly cleans
#   up leftover data
#    - Create a session
#    - POST on /leftover_data with 52 bytes of data (data includes
#      \r\n)(the handler only
#      reads first 10 bytes and returns them, leaving the rest of the
#      bytes unread)
#    - GET on /hello (should return 'Hello World')
#    - POST on /false_uri with 52  bytes of data (data includes \r\n)
#      (should return HTTP 404)
#    - GET on /hello (should return 'Hello World')
#
# - Test HTTPd Asynchronous response
#   - Create a session
#   - GET on /async_data
#   - returns 'Hello World!' as a response
#   - the handler schedules an async response, which generates a second
#     response 'Hello Double World!'
#
# - Spillover test
#    - Create max supported sessions with the web server
#    - GET /hello on all the sessions (should return Hello World)
#    - Create one more session, this should fail
#    - GET /hello on all the sessions (should return Hello World)
#
# - Timeout test
#    - Create a session and only Send 'GE' on the same (simulates a
#      client that left the network halfway through a request)
#    - Wait for recv-wait-timeout
#    - Server should automatically close the socket


############# TODO TESTS #############

# 3. Stress Tests
#
# - httperf
#     - Run the following httperf command:
#     httperf --server=10.31.130.126 --wsess=8,50,0.5 --rate 8 --burst-length 2
#
#     - The above implies that the test suite will open
#           - 8 simultaneous connections with the server
#           - the rate of opening the sessions will be 8 per sec. So in our
#             case, a new connection will be opened every 0.2 seconds for 1 second
#           - The burst length 2 indicates that 2 requests will be sent
#             simultaneously on the same connection in a single go
#           - 0.5 seconds is the time between sending out 2 bursts
#           - 50 is the total number of requests that will be sent out
#
#     - So in the above example, the test suite will open 8
#       connections, each separated by 0.2 seconds. On each connection
#       it will send 2 requests in a single burst. The bursts on a
#       single connection will be separated by 0.5 seconds. A total of
#       25 bursts (25 x 2 = 50) will be sent out

# 4. Leak Tests
# - Simple Leak test
#    - Simple GET on /hello/restart (returns success, stop web server, measures leaks, restarts webserver)
#    - Simple GET on /hello/restart_results (returns the leak results)
# - Leak test with open sockets
#    - Open 8 sessions
#    - Simple GET on /hello/restart (returns success, stop web server,
#      measures leaks, restarts webserver)
#    - All sockets should get closed
#    - Simple GET on /hello/restart_results (returns the leak results)


from __future__ import division
from __future__ import print_function
from future import standard_library
standard_library.install_aliases()
from builtins import str
from builtins import range
from builtins import object
import threading
import socket
import time
import argparse
import http.client
import sys
import string
import random

_verbose_ = False

class Session(object):
    def __init__(self, addr, port, timeout = 15):
        self.client = socket.create_connection((addr, int(port)), timeout = timeout)
        self.target = addr
        self.status = 0
        self.encoding = ''
        self.content_type = ''
        self.content_len = 0

    def send_err_check(self, request, data=None):
        rval = True
        try:
            self.client.sendall(request.encode());
            if data:
                self.client.sendall(data.encode())
        except socket.error as err:
            self.client.close()
            print("Socket Error in send :", err)
            rval = False
        return rval

    def send_get(self, path, headers=None):
        request = "GET " + path + " HTTP/1.1\r\nHost: " + self.target
        if headers:
            for field, value in headers.items():
                request += "\r\n"+field+": "+value
        request += "\r\n\r\n"
        return self.send_err_check(request)

    def send_put(self, path, data, headers=None):
        request = "PUT " + path +  " HTTP/1.1\r\nHost: " + self.target
        if headers:
            for field, value in headers.items():
                request += "\r\n"+field+": "+value
        request += "\r\nContent-Length: " + str(len(data)) +"\r\n\r\n"
        return self.send_err_check(request, data)

    def send_post(self, path, data, headers=None):
        request = "POST " + path +  " HTTP/1.1\r\nHost: " + self.target
        if headers:
            for field, value in headers.items():
                request += "\r\n"+field+": "+value
        request += "\r\nContent-Length: " + str(len(data)) +"\r\n\r\n"
        return self.send_err_check(request, data)

    def read_resp_hdrs(self):
        try:
            state = 'nothing'
            resp_read = ''
            while True:
                char = self.client.recv(1).decode()
                if char == '\r' and state == 'nothing':
                    state = 'first_cr'
                elif char == '\n' and state == 'first_cr':
                    state = 'first_lf'
                elif char == '\r' and state == 'first_lf':
                    state = 'second_cr'
                elif char == '\n' and state == 'second_cr':
                    state = 'second_lf'
                else:
                    state = 'nothing'
                resp_read += char
                if state == 'second_lf':
                    break
            # Handle first line
            line_hdrs = resp_read.splitlines()
            line_comp = line_hdrs[0].split()
            self.status = line_comp[1]
            del line_hdrs[0]
            self.encoding = ''
            self.content_type = ''
            headers = dict()
            # Process other headers
            for h in range(len(line_hdrs)):
                line_comp = line_hdrs[h].split(':')
                if line_comp[0] == 'Content-Length':
                    self.content_len = int(line_comp[1])
                if line_comp[0] == 'Content-Type':
                    self.content_type = line_comp[1].lstrip()
                if line_comp[0] == 'Transfer-Encoding':
                    self.encoding = line_comp[1].lstrip()
                if len(line_comp) == 2:
                    headers[line_comp[0]] = line_comp[1].lstrip()
            return headers
        except socket.error as err:
            self.client.close()
            print("Socket Error in recv :", err)
            return None

    def read_resp_data(self):
        try:
            read_data = ''
            if self.encoding != 'chunked':
                while len(read_data) != self.content_len:
                    read_data += self.client.recv(self.content_len).decode()
            else:
                chunk_data_buf = ''
                while (True):
                    # Read one character into temp  buffer
                    read_ch = self.client.recv(1)
                    # Check CRLF
                    if (read_ch == '\r'):
                        read_ch = self.client.recv(1).decode()
                        if (read_ch == '\n'):
                            # If CRLF decode length of chunk
                            chunk_len = int(chunk_data_buf, 16)
                            # Keep adding to contents
                            self.content_len += chunk_len
                            rem_len = chunk_len
                            while (rem_len):
                                new_data = self.client.recv(rem_len)
                                read_data += new_data
                                rem_len -= len(new_data)
                            chunk_data_buf = ''
                            # Fetch remaining CRLF
                            if self.client.recv(2) != "\r\n":
                                # Error in packet
                                print("Error in chunked data")
                                return None
                            if not chunk_len:
                                # If last chunk
                                break
                            continue
                        chunk_data_buf += '\r'
                    # If not CRLF continue appending
                    # character to chunked data buffer
                    chunk_data_buf += read_ch
            return read_data
        except socket.error as err:
            self.client.close()
            print("Socket Error in recv :", err)
            return None

    def close(self):
        self.client.close()

def test_val(text, expected, received):
    if expected != received:
        print(" Fail!")
        print("  [reason] " + text + ":")
        print("        expected: " + str(expected))
        print("        received: " + str(received))
        return False
    return True

class adder_thread (threading.Thread):
    def __init__(self, id, dut, port):
        threading.Thread.__init__(self)
        self.id = id
        self.dut = dut
        self.depth = 3
        self.session = Session(dut, port)

    def run(self):
        self.response = []

        # Pipeline 3 requests
        if (_verbose_):
            print("   Thread: Using adder start " + str(self.id))

        for _ in range(self.depth):
            self.session.send_post('/adder', str(self.id))
            time.sleep(2)

        for _ in range(self.depth):
            self.session.read_resp_hdrs()
            self.response.append(self.session.read_resp_data())

    def adder_result(self):
        if len(self.response) != self.depth:
            print("Error : missing response packets")
            return False
        for i in range(len(self.response)):
            if not test_val("Thread" + str(self.id) + " response[" + str(i) + "]",
                            str(self.id * (i + 1)), str(self.response[i])):
                return False
        return True

    def close(self):
        self.session.close()

def get_hello(dut, port):
    # GET /hello should return 'Hello World!'
    print("[test] GET /hello returns 'Hello World!' =>", end=' ')
    conn = http.client.HTTPConnection(dut, int(port), timeout=15)
    conn.request("GET", "/hello")
    resp = conn.getresponse()
    if not test_val("status_code", 200, resp.status):
        conn.close()
        return False
    if not test_val("data", "Hello World!", resp.read().decode()):
        conn.close()
        return False
    if not test_val("data", "text/html", resp.getheader('Content-Type')):
        conn.close()
        return False
    print("Success")
    conn.close()
    return True

def put_hello(dut, port):
    # PUT /hello returns 405'
    print("[test] PUT /hello returns 405 =>", end=' ')
    conn = http.client.HTTPConnection(dut, int(port), timeout=15)
    conn.request("PUT", "/hello", "Hello")
    resp = conn.getresponse()
    if not test_val("status_code", 405, resp.status):
        conn.close()
        return False
    print("Success")
    conn.close()
    return True

def post_hello(dut, port):
    # POST /hello returns 405'
    print("[test] POST /hello returns 404 =>", end=' ')
    conn = http.client.HTTPConnection(dut, int(port), timeout=15)
    conn.request("POST", "/hello", "Hello")
    resp = conn.getresponse()
    if not test_val("status_code", 405, resp.status):
        conn.close()
        return False
    print("Success")
    conn.close()
    return True

def post_echo(dut, port):
    # POST /echo echoes data'
    print("[test] POST /echo echoes data =>", end=' ')
    conn = http.client.HTTPConnection(dut, int(port), timeout=15)
    conn.request("POST", "/echo", "Hello")
    resp = conn.getresponse()
    if not test_val("status_code", 200, resp.status):
        conn.close()
        return False
    if not test_val("data", "Hello", resp.read().decode()):
        conn.close()
        return False
    print("Success")
    conn.close()
    return True

def put_echo(dut, port):
    # PUT /echo echoes data'
    print("[test] PUT /echo echoes data =>", end=' ')
    conn = http.client.HTTPConnection(dut, int(port), timeout=15)
    conn.request("PUT", "/echo", "Hello")
    resp = conn.getresponse()
    if not test_val("status_code", 200, resp.status):
        conn.close()
        return False
    if not test_val("data", "Hello", resp.read().decode()):
        conn.close()
        return False
    print("Success")
    conn.close()
    return True

def get_echo(dut, port):
    # GET /echo returns 404'
    print("[test] GET /echo returns 405 =>", end=' ')
    conn = http.client.HTTPConnection(dut, int(port), timeout=15)
    conn.request("GET", "/echo")
    resp = conn.getresponse()
    if not test_val("status_code", 405, resp.status):
        conn.close()
        return False
    print("Success")
    conn.close()
    return True

def get_hello_type(dut, port):
    # GET /hello/type_html returns text/html as Content-Type'
    print("[test] GET /hello/type_html has Content-Type of text/html =>", end=' ')
    conn = http.client.HTTPConnection(dut, int(port), timeout=15)
    conn.request("GET", "/hello/type_html")
    resp = conn.getresponse()
    if not test_val("status_code", 200, resp.status):
        conn.close()
        return False
    if not test_val("data", "Hello World!", resp.read().decode()):
        conn.close()
        return False
    if not test_val("data", "text/html", resp.getheader('Content-Type')):
        conn.close()
        return False
    print("Success")
    conn.close()
    return True

def get_hello_status(dut, port):
    # GET /hello/status_500 returns status 500'
    print("[test] GET /hello/status_500 returns status 500 =>", end=' ')
    conn = http.client.HTTPConnection(dut, int(port), timeout=15)
    conn.request("GET", "/hello/status_500")
    resp = conn.getresponse()
    if not test_val("status_code", 500, resp.status):
        conn.close()
        return False
    print("Success")
    conn.close()
    return True

def get_false_uri(dut, port):
    # GET /false_uri returns status 404'
    print("[test] GET /false_uri returns status 404 =>", end=' ')
    conn = http.client.HTTPConnection(dut, int(port), timeout=15)
    conn.request("GET", "/false_uri")
    resp = conn.getresponse()
    if not test_val("status_code", 404, resp.status):
        conn.close()
        return False
    print("Success")
    conn.close()
    return True

def parallel_sessions_adder(dut, port, max_sessions):
    # POSTs on /adder in parallel sessions
    print("[test] POST {pipelined} on /adder in " + str(max_sessions) + " sessions =>", end=' ')
    t = []
    # Create all sessions
    for i in range(max_sessions):
        t.append(adder_thread(i, dut, port))

    for i in range(len(t)):
        t[i].start()

    for i in range(len(t)):
        t[i].join()

    res = True
    for i in range(len(t)):
        if not test_val("Thread" + str(i) + " Failed", t[i].adder_result(), True):
            res = False
        t[i].close()
    if (res):
        print("Success")
    return res

def async_response_test(dut, port):
    # Test that an asynchronous work is executed in the HTTPD's context
    # This is tested by reading two responses over the same session
    print("[test] Test HTTPD Work Queue (Async response) =>", end=' ')
    s = Session(dut, port)

    s.send_get('/async_data')
    s.read_resp_hdrs()
    if not test_val("First Response", "Hello World!", s.read_resp_data()):
        s.close()
        return False
    s.read_resp_hdrs()
    if not test_val("Second Response", "Hello Double World!", s.read_resp_data()):
        s.close()
        return False
    s.close()
    print("Success")
    return True

def leftover_data_test(dut, port):
    # Leftover data in POST is purged (valid and invalid URIs)
    print("[test] Leftover data in POST is purged (valid and invalid URIs) =>", end=' ')
    s = http.client.HTTPConnection(dut + ":" + port, timeout=15)

    s.request("POST", url='/leftover_data', body="abcdefghijklmnopqrstuvwxyz\r\nabcdefghijklmnopqrstuvwxyz")
    resp = s.getresponse()
    if not test_val("Partial data", "abcdefghij", resp.read().decode()):
        s.close()
        return False

    s.request("GET", url='/hello')
    resp = s.getresponse()
    if not test_val("Hello World Data", "Hello World!", resp.read().decode()):
        s.close()
        return False

    s.request("POST", url='/false_uri', body="abcdefghijklmnopqrstuvwxyz\r\nabcdefghijklmnopqrstuvwxyz")
    resp = s.getresponse()
    if not test_val("False URI Status", str(404), str(resp.status)):
        s.close()
        return False
    resp.read()

    s.request("GET", url='/hello')
    resp = s.getresponse()
    if not test_val("Hello World Data", "Hello World!", resp.read().decode()):
        s.close()
        return False

    s.close()
    print("Success")
    return True

def spillover_session(dut, port, max_sess):
    # Session max_sess_sessions + 1 is rejected
    print("[test] Session max_sess_sessions (" + str(max_sess) + ") + 1 is rejected =>", end=' ')
    s = []
    _verbose_ = True
    for i in range(max_sess + 1):
        if (_verbose_):
            print("Executing " + str(i))
        try:
            a = http.client.HTTPConnection(dut + ":" + port, timeout=15)
            a.request("GET", url='/hello')
            resp = a.getresponse()
            if not test_val("Connection " + str(i), "Hello World!", resp.read().decode()):
                a.close()
                break
            s.append(a)
        except:
            if (_verbose_):
                print("Connection " + str(i) + " rejected")
            a.close()
            break

    # Close open connections
    for a in s:
        a.close()

    # Check if number of connections is equal to max_sess
    print(["Fail","Success"][len(s) == max_sess])
    return (len(s) == max_sess)

def recv_timeout_test(dut, port):
    print("[test] Timeout occurs if partial packet sent =>", end=' ')
    s = Session(dut, port)
    s.client.sendall(b"GE")
    s.read_resp_hdrs()
    resp = s.read_resp_data()
    if not test_val("Request Timeout", "Server closed this connection", resp):
        s.close()
        return False
    s.close()
    print("Success")
    return True

def packet_size_limit_test(dut, port, test_size):
    print("[test] send size limit test =>", end=' ')
    retry = 5
    while (retry):
        retry -= 1
        print("data size = ", test_size)
        s = http.client.HTTPConnection(dut + ":" + port, timeout=15)
        random_data = ''.join(string.printable[random.randint(0,len(string.printable))-1] for _ in list(range(test_size)))
        path = "/echo"
        s.request("POST", url=path, body=random_data)
        resp = s.getresponse()
        if not test_val("Error", "200", str(resp.status)):
            if test_val("Error", "500", str(resp.status)):
                print("Data too large to be allocated")
                test_size = test_size//10
            else:
                print("Unexpected error")
            s.close()
            print("Retry...")
            continue
        resp = resp.read().decode()
        result = (resp == random_data)
        if not result:
            test_val("Data size", str(len(random_data)), str(len(resp)))
            s.close()
            print("Retry...")
            continue
        s.close()
        print("Success")
        return True
    print("Failed")
    return False

def code_500_server_error_test(dut, port):
    print("[test] 500 Server Error test =>", end=' ')
    s = Session(dut, port)
    # Sending a very large content length will cause malloc to fail
    content_len = 2**31
    s.client.sendall(("POST /echo HTTP/1.1\r\nHost: " + dut + "\r\nContent-Length: " + str(content_len) + "\r\n\r\nABCD").encode())
    s.read_resp_hdrs()
    resp = s.read_resp_data()
    if not test_val("Server Error", "500", s.status):
        s.close()
        return False
    s.close()
    print("Success")
    return True

def code_501_method_not_impl(dut, port):
    print("[test] 501 Method Not Implemented =>", end=' ')
    s = Session(dut, port)
    path = "/hello"
    s.client.sendall(("ABC " + path + " HTTP/1.1\r\nHost: " + dut + "\r\n\r\n").encode())
    s.read_resp_hdrs()
    resp = s.read_resp_data()
    # Presently server sends back 400 Bad Request
    #if not test_val("Server Error", "501", s.status):
        #s.close()
        #return False
    if not test_val("Server Error", "400", s.status):
        s.close()
        return False
    s.close()
    print("Success")
    return True

def code_505_version_not_supported(dut, port):
    print("[test] 505 Version Not Supported =>", end=' ')
    s = Session(dut, port)
    path = "/hello"
    s.client.sendall(("GET " + path + " HTTP/2.0\r\nHost: " + dut + "\r\n\r\n").encode())
    s.read_resp_hdrs()
    resp = s.read_resp_data()
    if not test_val("Server Error", "505", s.status):
        s.close()
        return False
    s.close()
    print("Success")
    return True

def code_400_bad_request(dut, port):
    print("[test] 400 Bad Request =>", end=' ')
    s = Session(dut, port)
    path = "/hello"
    s.client.sendall(("XYZ " + path + " HTTP/1.1\r\nHost: " + dut + "\r\n\r\n").encode())
    s.read_resp_hdrs()
    resp = s.read_resp_data()
    if not test_val("Client Error", "400", s.status):
        s.close()
        return False
    s.close()
    print("Success")
    return True

def code_404_not_found(dut, port):
    print("[test] 404 Not Found =>", end=' ')
    s = Session(dut, port)
    path = "/dummy"
    s.client.sendall(("GET " + path + " HTTP/1.1\r\nHost: " + dut + "\r\n\r\n").encode())
    s.read_resp_hdrs()
    resp = s.read_resp_data()
    if not test_val("Client Error", "404", s.status):
        s.close()
        return False
    s.close()
    print("Success")
    return True

def code_405_method_not_allowed(dut, port):
    print("[test] 405 Method Not Allowed =>", end=' ')
    s = Session(dut, port)
    path = "/hello"
    s.client.sendall(("POST " + path + " HTTP/1.1\r\nHost: " + dut + "\r\n\r\n").encode())
    s.read_resp_hdrs()
    resp = s.read_resp_data()
    if not test_val("Client Error", "405", s.status):
        s.close()
        return False
    s.close()
    print("Success")
    return True

def code_408_req_timeout(dut, port):
    print("[test] 408 Request Timeout =>", end=' ')
    s = Session(dut, port)
    s.client.sendall(("POST /echo HTTP/1.1\r\nHost: " + dut + "\r\nContent-Length: 10\r\n\r\nABCD").encode())
    s.read_resp_hdrs()
    resp = s.read_resp_data()
    if not test_val("Client Error", "408", s.status):
        s.close()
        return False
    s.close()
    print("Success")
    return True

def code_411_length_required(dut, port):
    print("[test] 411 Length Required =>", end=' ')
    s = Session(dut, port)
    path = "/echo"
    s.client.sendall(("POST " + path + " HTTP/1.1\r\nHost: " + dut + "\r\nContent-Type: text/plain\r\nTransfer-Encoding: chunked\r\n\r\n").encode())
    s.read_resp_hdrs()
    resp = s.read_resp_data()
    # Presently server sends back 400 Bad Request
    #if not test_val("Client Error", "411", s.status):
        #s.close()
        #return False
    if not test_val("Client Error", "400", s.status):
        s.close()
        return False
    s.close()
    print("Success")
    return True

def send_getx_uri_len(dut, port, length):
    s = Session(dut, port)
    method = "GET "
    version = " HTTP/1.1\r\n"
    path = "/"+"x"*(length - len(method) - len(version) - len("/"))
    s.client.sendall(method.encode())
    time.sleep(1)
    s.client.sendall(path.encode())
    time.sleep(1)
    s.client.sendall((version + "Host: " + dut + "\r\n\r\n").encode())
    s.read_resp_hdrs()
    resp = s.read_resp_data()
    s.close()
    return s.status

def code_414_uri_too_long(dut, port, max_uri_len):
    print("[test] 414 URI Too Long =>", end=' ')
    status = send_getx_uri_len(dut, port, max_uri_len)
    if not test_val("Client Error", "404", status):
        return False
    status = send_getx_uri_len(dut, port, max_uri_len + 1)
    if not test_val("Client Error", "414", status):
        return False
    print("Success")
    return True

def send_postx_hdr_len(dut, port, length):
    s = Session(dut, port)
    path = "/echo"
    host = "Host: " + dut
    custom_hdr_field = "\r\nCustom: "
    custom_hdr_val = "x"*(length - len(host) - len(custom_hdr_field) - len("\r\n\r\n") + len("0"))
    request = ("POST " + path + " HTTP/1.1\r\n" + host + custom_hdr_field + custom_hdr_val + "\r\n\r\n").encode()
    s.client.sendall(request[:length//2])
    time.sleep(1)
    s.client.sendall(request[length//2:])
    hdr = s.read_resp_hdrs()
    resp = s.read_resp_data()
    s.close()
    if "Custom" in hdr:
        return (hdr["Custom"] == custom_hdr_val), resp
    return False, s.status

def code_431_hdr_too_long(dut, port, max_hdr_len):
    print("[test] 431 Header Too Long =>", end=' ')
    res, status = send_postx_hdr_len(dut, port, max_hdr_len)
    if not res:
        return False
    res, status = send_postx_hdr_len(dut, port, max_hdr_len + 1)
    if not test_val("Client Error", "431", status):
        return False
    print("Success")
    return True

def test_upgrade_not_supported(dut, port):
    print("[test] Upgrade Not Supported =>", end=' ')
    s = Session(dut, port)
    path = "/hello"
    s.client.sendall(("OPTIONS * HTTP/1.1\r\nHost:" + dut + "\r\nUpgrade: TLS/1.0\r\nConnection: Upgrade\r\n\r\n").encode())
    s.read_resp_hdrs()
    resp = s.read_resp_data()
    if not test_val("Client Error", "200", s.status):
        s.close()
        return False
    s.close()
    print("Success")
    return True

if __name__ == '__main__':
    ########### Execution begins here...
    # Configuration
    # Max number of threads/sessions
    max_sessions = 7
    max_uri_len = 512
    max_hdr_len = 512

    parser = argparse.ArgumentParser(description='Run HTTPD Test')
    parser.add_argument('-4','--ipv4', help='IPv4 address')
    parser.add_argument('-6','--ipv6', help='IPv6 address')
    parser.add_argument('-p','--port', help='Port')
    args = vars(parser.parse_args())

    dut4 = args['ipv4']
    dut6 = args['ipv6']
    port = args['port']
    dut  = dut4

    _verbose_ = True

    print("### Basic HTTP Client Tests")
    get_hello(dut, port)
    post_hello(dut, port)
    put_hello(dut, port)
    post_echo(dut, port)
    get_echo(dut, port)
    put_echo(dut, port)
    get_hello_type(dut, port)
    get_hello_status(dut, port)
    get_false_uri(dut, port)

    print("### Error code tests")
    code_500_server_error_test(dut, port)
    code_501_method_not_impl(dut, port)
    code_505_version_not_supported(dut, port)
    code_400_bad_request(dut, port)
    code_404_not_found(dut, port)
    code_405_method_not_allowed(dut, port)
    code_408_req_timeout(dut, port)
    code_414_uri_too_long(dut, port, max_uri_len)
    code_431_hdr_too_long(dut, port, max_hdr_len)
    test_upgrade_not_supported(dut, port)

    # Not supported yet (Error on chunked request)
    ###code_411_length_required(dut, port)

    print("### Sessions and Context Tests")
    parallel_sessions_adder(dut, port, max_sessions)
    leftover_data_test(dut, port)
    async_response_test(dut, port)
    spillover_session(dut, port, max_sessions)
    recv_timeout_test(dut, port)
    packet_size_limit_test(dut, port, 50*1024)
    get_hello(dut, port)

    sys.exit()
