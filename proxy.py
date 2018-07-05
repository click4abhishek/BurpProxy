# -*- coding: utf-8 -*-
import sys
import os
import socket
import ssl
import select
import httplib
import urlparse
import threading
import gzip
import zlib
import time
import json
import re
from BaseHTTPServer import HTTPServer, BaseHTTPRequestHandler
from SocketServer import ThreadingMixIn
from cStringIO import StringIO
from subprocess import Popen, PIPE
from HTMLParser import HTMLParser
import base64
from pathlib import Path
from termcolor import colored
import traceback
sys.path.insert(0, './Modules')
import enc_dec_aes
import enc_dec_des
import enc_dec_des3
import EncDec

def with_color(c, s):
    return "\x1b[%dm%s\x1b[0m" % (c, s)

def join_with_script_dir(path):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), path)


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    address_family = socket.AF_INET6
    daemon_threads = True

    def handle_error(self, request, client_address):
        # surpress socket/ssl related errors
        cls, e = sys.exc_info()[:2]
        if cls is socket.error or cls is ssl.SSLError:
            pass
        else:
            return HTTPServer.handle_error(self, request, client_address)


class ProxyRequestHandler(BaseHTTPRequestHandler):
    cakey = join_with_script_dir('ca.key')
    cacert = join_with_script_dir('ca.crt')
    certkey = join_with_script_dir('cert.key')
    certdir = join_with_script_dir('certs/')
    timeout = 5
    lock = threading.Lock()

    def __init__(self, *args, **kwargs):
        self.tls = threading.local()
        self.tls.conns = {}

        BaseHTTPRequestHandler.__init__(self, *args, **kwargs)

    def log_error(self, format, *args):
        # surpress "Request timed out: timeout('timed out',)"
        if isinstance(args[0], socket.timeout):
            return

        self.log_message(format, *args)

    def do_CONNECT(self):
        if os.path.isfile(self.cakey) and os.path.isfile(self.cacert) and os.path.isfile(self.certkey) and os.path.isdir(self.certdir):
            self.connect_intercept()
        else:
            self.connect_relay()

    def connect_intercept(self):
        hostname = self.path.split(':')[0]
        certpath = "%s/%s.crt" % (self.certdir.rstrip('/'), hostname)

        with self.lock:
            if not os.path.isfile(certpath):
                epoch = "%d" % (time.time() * 1000)
                p1 = Popen(["openssl", "req", "-new", "-key", self.certkey, "-subj", "/CN=%s" % hostname], stdout=PIPE)
                p2 = Popen(["openssl", "x509", "-req", "-days", "3650", "-CA", self.cacert, "-CAkey", self.cakey, "-set_serial", epoch, "-out", certpath], stdin=p1.stdout, stderr=PIPE)
                p2.communicate()

        self.wfile.write("%s %d %s\r\n" % (self.protocol_version, 200, 'Connection Established'))
        self.end_headers()

        self.connection = ssl.wrap_socket(self.connection, keyfile=self.certkey, certfile=certpath, server_side=True)
        self.rfile = self.connection.makefile("rb", self.rbufsize)
        self.wfile = self.connection.makefile("wb", self.wbufsize)

        conntype = self.headers.get('Proxy-Connection', '')
        if self.protocol_version == "HTTP/1.1" and conntype.lower() != 'close':
            self.close_connection = 0
        else:
            self.close_connection = 1

    def connect_relay(self):
        address = self.path.split(':', 1)
        address[1] = int(address[1]) or 443
        try:
            s = socket.create_connection(address, timeout=self.timeout)
        except Exception as e:
            self.send_error(502)
            return
        self.send_response(200, 'Connection Established')
        self.end_headers()

        conns = [self.connection, s]
        self.close_connection = 0
        while not self.close_connection:
            rlist, wlist, xlist = select.select(conns, [], conns, self.timeout)
            if xlist or not rlist:
                break
            for r in rlist:
                other = conns[1] if r is conns[0] else conns[0]
                data = r.recv(8192)
                if not data:
                    self.close_connection = 1
                    break
                other.sendall(data)

    def do_GET(self):
        if self.path == 'http://proxy2.test/':
            self.send_cacert()
            return

        req = self
        content_length = int(req.headers.get('Content-Length', 0))
        req_body = self.rfile.read(content_length) if content_length else None

        global flag_for_url
        global target

        if flag_for_url==0:
            target=req.headers['Host']
            flag_for_url=1

        if req.path[0] == '/':
            if isinstance(self.connection, ssl.SSLSocket):
                req.path = "https://%s%s" % (target, req.path)
            else:
                req.path = "http://%s%s" % (target, req.path)

        req_body_modified = self.request_handler(req, req_body)
        if req_body_modified is False:
            self.send_error(403)
            return
        elif req_body_modified is not None:
            req_body = req_body_modified
            req.headers['Content-length'] = str(len(req_body))

        u = urlparse.urlsplit(req.path)
        scheme, netloc, path = u.scheme, u.netloc, (u.path + '?' + u.query if u.query else u.path)
        assert scheme in ('http', 'https')
        if netloc:
            req.headers['Host'] = netloc
        setattr(req, 'headers', self.filter_headers(req.headers))

        try:
            origin = (scheme, netloc)
            if not origin in self.tls.conns:
                if scheme == 'https':
                    if encrdecr=='d':
                        self.tls.conns[origin] = httplib.HTTPSConnection("localhost", 3333)
                        conn = self.tls.conns[origin]
                        conn.set_tunnel(netloc)
                    else:
                        self.tls.conns[origin] = httplib.HTTPSConnection(netloc, timeout=self.timeout)
                        conn = self.tls.conns[origin]
                else:
                    if encrdecr=='d':
                        self.tls.conns[origin] = httplib.HTTPConnection("localhost", 3333)
                        conn = self.tls.conns[origin]
                        conn.set_tunnel(netloc)
                    else:
                        self.tls.conns[origin] = httplib.HTTPConnection(netloc, timeout=self.timeout)
                        conn = self.tls.conns[origin]
            
            conn.request(self.command, path, req_body, dict(req.headers))
            res = conn.getresponse()

            version_table = {10: 'HTTP/1.0', 11: 'HTTP/1.1'}
            setattr(res, 'headers', res.msg)
            setattr(res, 'response_version', version_table[res.version])

            # support streaming
            if not 'Content-Length' in res.headers and 'no-store' in res.headers.get('Cache-Control', ''):
                self.response_handler(res,res_body)
                setattr(res, 'headers', self.filter_headers(res.headers))
                self.relay_streaming(res)
                with self.lock:
                    self.save_handler(req, req_body, res, '')
                return

            res_body = res.read()
        except Exception as e:
            if origin in self.tls.conns:
                del self.tls.conns[origin]
            self.send_error(502)
            return

        content_encoding = res.headers.get('Content-Encoding', 'identity')
        res_body_plain = self.decode_content_body(res_body, content_encoding)

        res_body_modified = self.response_handler(res, res_body_plain)
        if res_body_modified is False:
            self.send_error(403)
            return
        
        elif res_body_modified is not None:
            res_body_plain = res_body_modified
            res_body = self.encode_content_body(res_body_plain, content_encoding)
            #res.headers['Content-Length'] = str(len(res_body))

        setattr(res, 'headers', self.filter_headers(res.headers))

        self.wfile.write("%s %d %s\r\n" % (self.protocol_version, res.status, res.reason))
        for line in res.headers.headers:
            if "Content-Length" not in line:
                self.wfile.write(line)
        #if res_body_modified is not None:
        self.wfile.write("Content-Length: "+str(len(res_body))+"\r\n")
        self.end_headers()
        self.wfile.write(res_body)
        self.wfile.flush()

        with self.lock:
            self.save_handler(req, req_body, res, res_body_plain)

    def relay_streaming(self, res):
        self.wfile.write("%s %d %s\r\n" % (self.protocol_version, res.status, res.reason))
        for line in res.headers.headers:
            self.wfile.write(line)
        self.end_headers()
        try:
            while True:
                chunk = res.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
            self.wfile.flush()
        except socket.error:
            # connection closed by client
            pass

    do_HEAD = do_GET
    do_POST = do_GET
    do_PUT = do_GET
    do_DELETE = do_GET
    do_OPTIONS = do_GET

    def filter_headers(self, headers):
        # http://tools.ietf.org/html/rfc2616#section-13.5.1
        hop_by_hop = ('connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailers', 'transfer-encoding', 'upgrade')
        for k in hop_by_hop:
            del headers[k]

        # accept only supported encodings
        if 'Accept-Encoding' in headers:
            ae = headers['Accept-Encoding']
            filtered_encodings = [x for x in re.split(r',\s*', ae) if x in ('identity', 'gzip', 'x-gzip', 'deflate')]
            headers['Accept-Encoding'] = ', '.join(filtered_encodings)

        return headers

    def encode_content_body(self, text, encoding):
        if encoding == 'identity':
            data = text
        elif encoding in ('gzip', 'x-gzip'):
            io = StringIO()
            with gzip.GzipFile(fileobj=io, mode='wb') as f:
                f.write(text)
            data = io.getvalue()
        elif encoding == 'deflate':
            data = zlib.compress(text)
        else:
            raise Exception("Unknown Content-Encoding: %s" % encoding)
        return data

    def decode_content_body(self, data, encoding):
        if encoding == 'identity':
            text = data
        elif encoding in ('gzip', 'x-gzip'):
            io = StringIO(data)
            with gzip.GzipFile(fileobj=io) as f:
                text = f.read()
        elif encoding == 'deflate':
            try:
                text = zlib.decompress(data)
            except zlib.error:
                text = zlib.decompress(data, -zlib.MAX_WBITS)
        else:
            raise Exception("Unknown Content-Encoding: %s" % encoding)
        return text

    def send_cacert(self):
        with open(self.cacert, 'rb') as f:
            data = f.read()

        self.wfile.write("%s %d %s\r\n" % (self.protocol_version, 200, 'OK'))
        self.send_header('Content-Type', 'application/x-x509-ca-cert')
        self.send_header('Content-Length', len(data))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(data)

    def print_info(self, req, req_body, res, res_body):
        def parse_qsl(s):
            return '\n'.join("%-20s %s" % (k, v) for k, v in urlparse.parse_qsl(s, keep_blank_values=True))

        req_header_text = "%s %s %s\n%s" % (req.command, req.path, req.request_version, req.headers)
        res_header_text = "%s %d %s\n%s" % (res.response_version, res.status, res.reason, res.headers)

        print with_color(33, req_header_text)

        u = urlparse.urlsplit(req.path)
        if u.query:
            query_text = parse_qsl(u.query)
            print with_color(32, "==== QUERY PARAMETERS ====\n%s\n" % query_text)

        cookie = req.headers.get('Cookie', '')
        if cookie:
            cookie = parse_qsl(re.sub(r';\s*', '&', cookie))
            print with_color(32, "==== COOKIE ====\n%s\n" % cookie)

        auth = req.headers.get('Authorization', '')
        if auth.lower().startswith('basic'):
            token = auth.split()[1].decode('base64')
            print with_color(31, "==== BASIC AUTH ====\n%s\n" % token)

        if req_body is not None:
            req_body_text = None
            content_type = req.headers.get('Content-Type', '')

            if content_type.startswith('application/x-www-form-urlencoded'):
                req_body_text = parse_qsl(req_body)
            elif content_type.startswith('application/json'):
                try:
                    json_obj = json.loads(req_body)
                    json_str = json.dumps(json_obj, indent=2)
                    if json_str.count('\n') < 50:
                        req_body_text = json_str
                    else:
                        lines = json_str.splitlines()
                        req_body_text = "%s\n(%d lines)" % ('\n'.join(lines[:50]), len(lines))
                except ValueError:
                    req_body_text = req_body
            elif len(req_body) < 1024:
                req_body_text = req_body

            if req_body_text:
                print with_color(32, "==== REQUEST BODY ====\n%s\n" % req_body_text)

        print with_color(36, res_header_text)

        cookies = res.headers.getheaders('Set-Cookie')
        if cookies:
            cookies = '\n'.join(cookies)
            print with_color(31, "==== SET-COOKIE ====\n%s\n" % cookies)

        if res_body is not None:
            res_body_text = None
            content_type = res.headers.get('Content-Type', '')

            if content_type.startswith('application/json'):
                try:
                    json_obj = json.loads(res_body)
                    json_str = json.dumps(json_obj, indent=2)
                    if json_str.count('\n') < 50:
                        res_body_text = json_str
                    else:
                        lines = json_str.splitlines()
                        res_body_text = "%s\n(%d lines)" % ('\n'.join(lines[:50]), len(lines))
                except ValueError:
                    res_body_text = res_body
            elif content_type.startswith('text/html'):
                m = re.search(r'<title[^>]*>\s*([^<]+?)\s*</title>', res_body, re.I)
                if m:
                    h = HTMLParser()
                    print with_color(32, "==== HTML TITLE ====\n%s\n" % h.unescape(m.group(1).decode('utf-8')))
            elif content_type.startswith('text/') and len(res_body) < 1024:
                res_body_text = res_body

            if res_body_text:
                print with_color(32, "==== RESPONSE BODY ====\n%s\n" % res_body_text)

    #Function to extract IV
    def extractIV(self):
        iv = ""
        if ivf is None:
            ivobj = open('iv.txt', 'r')
            lines = ivobj.readlines()
            iv = lines[0] if encrdecr == 'e' else lines[1]
            ivobj.close()
        else:
            iv =ivf
        dec = "EncDec." + mode_enco + "Dec"
        iv = eval(dec)(iv)
        # print iv
        # print type(iv)
        return iv

    #Function to extract text to encrypt
    def getBodyEnc(self, requestString):
        return requestString

    #Function to optimise json dictionary strings 
    def extract_dic(self,a):
        global b 
        b =  ""
        global stack
        stack = []
        global i
        i=0
        while (i<len(a)):
            if (a[i]=='{'):
                b = b+ a[i]
                if (stack.count('{')!=0):
                    count =1
                    i =i +1
                    stack.append('{')
                    while count!=0:
                        if a[i] == '{':
                            b = b + a[i]
                            count = count +1
                            stack.append('{')
                        elif (a[i]== '\"'):
                            b = b + '\''
                        elif (a[i] == '}'):
                            b = b + a[i]
                            stack.pop(len(stack)-1)
                            count  = count -1
                        else:
                            b = b + a[i]
                        i =i+1
                    i = i-1
                else:
                    stack.append(a[i])
            elif (a[i]=='}'):
                b = b + a[i]
                stack.pop(len(stack)-1)
            else:
                b = b + a[i]
            i = i +1
        return b
    # Function to return content_type and req_body.
    # handles -text/plain, text/html, application/json, application/xml, text/xml, application/x-www-form-urlencoded
    # multipart/form-data (Think can be split about =), multipart/byteranges, application/xhtml+xml
    # def content_type(self, re, re_body):
    #     con_type=0
    #     if re_body is not None:
    #         re_body_text=None
    #         try: 
    #             content_type=re.headers.get('Content-Type','')

    #             if content_type.lower().startswith('application/json'):
                    
    #                 #Aastha's function will give json
    #                 print "hi"
    #                 re_body_text = self.extract_dic(re_body)
    #                 print re_body_text
    #                 con_type=1

    #             elif not (content_type.lower().startswith('application') or content_type.lower().startswith('text/xml')):
    #                 re_body_text=re_body
    #                 con_type=2

    #             elif content_type.lower().startswith('application/x') or content_type.lower().startswith('text/xml'):
    #                 re_body_text=re_body
    #                 con_type=3

    #             elif content_type.lower().startswith('application/x-www-form-urlencoded'):
    #                 re_body_text=re_body
    #                 con_type=4
    #             elif content_type.lower().startswith('multipart/form-data'):
    #                 re_body_text=re_body
    #                 con_type=5
    #             else:
    #                 print "in Else "
    #                 re_body_text=re_body
    #                 con_type=0
    #         except Exception:
    #             print "Caught an exception"
    #             con_type=0
    #             return re_body,con_type        
        # return re_body_text,con_type

    def encEntireBody(self,req_body,iv):
        req_body = self.encr(req_body,iv)
        return req_body

    def encr_json(self,req_body, iv,para ):
        print "ENCR_JSON"
        try:
            json_obj = json.loads(req_body)
            for ele in json_obj.keys(): 
                if ele in para:
                    print ele
                    temp = json_obj[ele]
                    temp= self.encr(temp,iv)
                    if ivf is None and ivs == '1':
                        temp = iv + temp
                    elif ivf is None and ivs == '2':
                        temp = temp + iv
                    temp = eval(mode_encod)(temp)
                    json_obj[ele] = temp
            json_str = json.dumps(json_obj, indent=2)
            if json_str.count('\n') < 50:
                req_body_text = json_str
            else:
                lines = json_str.splitlines()
                req_body_text = "%s\n(%d lines)" % ('\n'.join(lines[:50]), len(lines))
        except Exception:
            req_body_text = req_body
            print (traceback.format_exc())
        return req_body_text

    def encr_xml(self,re_body,iv,para):
        temp =""
        try:
            for i in para:
                i.replace('\n','')
            e = ET.fromstring(str(re_body))
            for elt in e.iter():
                if elt.tag in para and elt.text is not None:
                    elt.text=self.encr(elt.text,iv)
            #Check what to do about XML Version and encoding
            temp = ET.tostring(e, encoding='utf8', method='xml')
            if ivf is None and ivs == '1':
                temp = iv + temp
            elif ivf is None and ivs == '2':
                temp = temp + iv
            temp = eval(mode_encod)(temp)
        except Exception:
            print (traceback.format_exc())
            temp  = re_body
        return temp

    def encr_www(self,re_body,iv,para):

        elem_dic=dict(x.split('=') for x in re_body.split("&"))
        for i in elem_dic:
            if elem_dic[i] in para:
                elem_dic[i]=self.encr(elem_dic[i],iv)
        temp = ""
        for key,value in elem_dic:
            temp = temp + key + '=' + value + '&'
        temp1 = temp [:-1]
        if ivf is None and ivs == '1':
            temp1 = iv + temp1
        elif ivf is None and ivs == '2':
            temp1 = temp1 + iv
        temp = eval(mode_encod)(temp1)
        return temp1

    def encryption(self, req, req_body):
        #Input of parameters. ivNum contains the number of IVs in the request. If different parameters
        #contain different IV, then obtained while encryption
        iv = self.extractIV()
        print ("Now inside Encryption Function")
        print req_body
        req_body_text = None
        #Encryption of full request body:
        if (int(mxen)==1):
            req_body = getBodyEnc(req_body)
            req_body_text = encEntireBody(req_body,iv)

        #Encryption of Parameters: Encrypts all values only: 
        if (int(mxen)==2 or int(mxen) == 3):
            req_body = self.getBodyEnc(req_body)
            req_body, content_type = self.get_body_text(req,req_body)
            filePara = open('reqpara.txt','r') if encrdecr == 'e' else open('respara.txt','r')
            para = filePara.readlines()
            
            for i in range(len(para)):
                para[i]=para[i].replace("\n","")
            print("Changed Here")
            print para
            if content_type == 1:
                req_body_text = self.encr_json(req_body,iv,para)
            elif  content_type == 2:
                req_body_text = req_body #Insert code here
            elif content_type == 3:
                req_body_text = self.encr_xml(req_body,iv,para)
            elif content_type == 4:
                req_body_text = self.encr_www(req_body,iv,para)
            elif content_type == 5:
                req_body_text = req_body #Insert code here
            else:
                pass
                # #Handler if the content-type is not application/json. File 'reqHandler' is opened and request 
                # #is written. The user is required to encrypt the plaintext required and paste back in the file.
                # #'e': encryption. 'q': stop encryption loop
                # reqFile = open('reqHandler.dat', 'w+')
                # reqFile.write(str(req_body))
                # reqFile.close()
                # choice =raw_input("Enter e for encryption of text and q to exit")
                # while (choice!='q'):
                #     encText = raw_input("Enter text to encrypt")
                #     encText = self.getBodyEnc(encText)
                #     encText = self.encr(encText,iv)
                #     if (ivs == '1'):
                #         encText = iv + encText
                #     else:
                #         encText = encText + iv
                #     encText = eval(mode_encod)(encText)
                #     print encText
                #     choice = raw_input("Do you want to continue? e(encrypt)/q (quit)")
                # reqFile = open('reqHandler.dat', 'r')
                # req_body_text = reqFile.read()
                # reqFile.close()
            filePara.close()   
            
            print "final Return value:"+ req_body_text
            return req_body_text


    def request_handler(self, req, req_body):
        global flag_side
        if encrdecr == 'e':
            flag_side=1
            return self.encryption(req,req_body)
        else:
            flag_side=0
            return self.decrypt(req,req_body)

        
    #Encryption Functions. User has to pass the required parameteres only.
    def encr(self, msg,iv):
        if (cipMethod=='AES'):
            if (cmode == 'ECB'):
                return enc_dec_aes.aes_ecb_enc(keyf, msg, padding)
            elif (cmode == 'CBC'):
                return enc_dec_aes.aes_cbc_enc(keyf, msg, iv, padding)
            elif (cmode == 'CFB'):
                return enc_dec_aes.aes_cfb_enc(keyf, msg, iv, padding, int(segment_size))

        if (cipMethod == 'DES'):
            if (cmode == 'ECB'):
                return enc_dec_des.des_ecb_enc(keyf, msg, padding)
            elif (cmode == 'CBC'):
                return enc_dec_des.des_cbc_enc(keyf, msg, iv, padding)
            elif (cmode == 'CFB'):
                return enc_dec_des.des_cfb_enc(keyf, msg, iv, padding, int(segment_size))

        if (cipMethod == 'DES3'):
            if (cmode == 'ECB'):
                return enc_dec_des3.des3_ecb_enc(keyf, msg, padding)
            elif (cmode == 'CBC'):
                return enc_dec_des3.des3_cbc_enc(keyf, msg, iv, padding)
            elif (cmode == 'CFB'):
               return enc_dec_des3.des3_cfb_enc(keyf, msg, iv, padding, int(segment_size))      



    def get_body_text(self,re,re_body):
    # -text/plain .
    # -text/html .
    # --application/json .
    # --application/xml .
    # --text/xml .
    # --application/x-www-form-urlencoded
    # --multipart/form-data (Think can be split about =)
    # -multipart/byteranges .
    # --application/xhtml+xml

        con_type=0
        if re_body is not None:
            re_body_text=None
            content_type=re.headers.get('Content-Type','')

            if content_type.lower().startswith('application/json'):
                re_body_text=self.extract_dic(re_body)
                con_type=1

            elif not (content_type.lower().startswith('application') or content_type.lower().startswith('text/xml')):
                re_body_text=re_body
                con_type=2

            elif content_type.lower().startswith('application/x') or content_type.lower().startswith('text/xml'):
                re_body_text=re_body
                con_type=3

            elif content_type.lower().startswith('application/x-www-form-urlencoded'):
                re_body_text=re_body
                con_type=4
            elif content_type.lower().startswith('multipart/form-data'):
                re_body_text=re_body
                con_type=5
            else:
                re_body_text=re_body
                con_type=0
                
        return re_body_text,con_type


    def response_handler(self, res ,res_body):
        #key="441538f57b510c0512f594c213cc523c"
        global encrdecr
        if encrdecr=='e':
             res_body_text=self.decrypt(res,res_body)
        else:
             res_body_text=self.encryption(res,res_body)
        return res_body_text


    def decrypt(self,re,re_body):
        print("Inside Decryption Function")
        print(re_body)
        re_list=self.get_body_text(re,re_body)
        re_body_text=re_list[0]
        con_type=re_list[1]
        # print colored("\n-------------------------------------------------","green")
        # print colored("The re is printed below-",'green')
        # print(re_body_text)

        global cipMethod
        global cmode
        global keyf
        global mode_enco
        global mode_encod
        global padding
        global ivs
        global ivf
        global segment_size
        global mxen
        global encrdecr
        global flag_for_mul_inp
        global flag_side

        decryption_final='enc_dec_'+cipMethod.lower()+"."+cipMethod.lower()+"_"+cmode.lower()+"_dec"
        block=16 if "aes" in decryption_final else 8
        dkey=keyf
        mode=padding
        dencod="EncDec."+mode_enco+"Dec"
        #Asking for function specific parameters. No IV for ECB and Segment Size for CFB.  
        iv_info=ivs # 1 for start, 2 for end, None if no IV
        if iv_info is not None:
            iv_info='beg' if iv_info=='1' else 'end'
        #Segment size will only be asked if CFB Mode is there
        seg_size=segment_size

        def decryption(todecrypt):
            orignal=todecrypt
            try:
                decoded_value=eval(dencod)(todecrypt)
                print(len(decoded_value))
                if iv_info is not None:
                    if iv_info=='beg' or iv_info=='end':
                        #Extracting IV from Cipher Text
                        iv=decoded_value[0:block] if iv_info=='beg' else decoded_value[-block:]
                        ct=decoded_value[16:] if iv_info =='beg' else decoded_value[0:len(decoded_value)-block]
                        todecrypt=eval(decryption_final)(dkey,ct,iv,mode) if seg_size == None else eval(decryption_final)(dkey,ct,iv,mode,seg_size)
                        iv=eval(mode_encod)(iv)
                        file=open("iv.txt","w") if encrdecr=='d' else open("iv.txt","a")
                        file.write(str(iv)+"\n")
                        file.close()
                        flag_for_iv=1
                    elif iv_info is None and ivf is not None: 
                        iv=ivf
                        ct=decoded_value
                        print("Hi")
                        todecrypt=eval(decryption_final)(dkey,ct,iv,mode) if seg_size == None else eval(decryption_final)(dkey,ct,iv,mode,seg_size)   
                else:
                    #If this is called, then no IV was required. Meaning most probably ECB Mode.
                    todecrypt=eval(decryption_final)(dkey,decoded_value,mode)
                return todecrypt
            except Exception:
                return original

        def decrypt_json(parameters,re_body_text):
            original=re_body_text
            body=json.loads(re_body_text)      
            try:
                for i in parameters:
                    body[i]=decryption(body[i])
                return json.dumps(body,indent=2).replace("\\","")
            except Exception:
                return original

        def decrypt_xml(parameters,re_body_text):
            original=re_body_text
            try:
                e = ET.fromstring(str(re_body_text))
                for elt in e.iter():
                    if elt.tag in parameters and elt.text is not None:
                            elt.text=decryption(elt.text)
                #Check what to do about XML Version and encoding
                re_body_text=ET.tostring(e, encoding='utf8', method='xml')
                return re_body_text
            except Exception:
                return original

        def decrypt_www_form_urlencoded(parameters,re_body_text):
            original=re_body_text
            try:
                elem_dic=dict(x.split('=') for x in res_body_text.split("&"))
                for i in elem_dic:
                    if elem_dic[i] in parameters:
                        elem_dic[i]=decryption(elem_dic[i])
                new_text=""
                for key, value in elem_dic.iteritems():
                    new_text=new_text+key+"="+value+"&"
                new_text_final=new_text[:-1] #Removing last &
                return new_text_final
            except Exception:
                return original

        def decrypt_multipart_form(parameters,re_body_text):
            pass
        
        def fetch_parameters(list_parameters,flag_side):
            if flag_side==0:
                file=open("reqpara.txt","w")
            elif flag_side==1:
                file=open("respara.txt","w")
            for i in list_parameters:
                file.write(str(i+"\n"))
            file.close()
            if flag_side==0:
                file=open("reqpara.txt","r")
            elif flag_side==1:
                file=open("respara.txt","r")
            parameters=file.readlines()
            for i in range(0,len(parameters)):
                parameters[i]=parameters[i].replace("\n","")
            file.close()
            return parameters

        if mxen == '1':
            res_body_text=decryption(re_body_text)
            return re_body_text

        if mxen =='2':

            if con_type==2 or con_type==0:
                sys.exit('This object has no key value pair!')
            list_parameters=[]

            if con_type==1:
                original=re_body_text
                try:
                    body=json.loads(re_body_text)
                    for key in body:
                        list_parameters.append(key)
                    parameters=fetch_parameters(list_parameters,flag_side)
                    return decrypt_json(parameters,re_body_text)
                except Exception:
                    return original
                
            if con_type==3:
                original=re_body_text
                try:
                    e = ET.fromstring(str(re_body_text))
                    for elt in e.iter():
                        if elt.text is not None:
                            list_parameters.append(elt.tag)
                    parameters=fetch_parameters(list_parameters,flag_side)
                    return decrypt_xml(parameters,re_body_text)
                except Exception:
                    return orignal

            if con_type==4:
                original=re_body_text
                try:
                    elem_dic=dict(x.split('=') for x in res_body_text.split("&"))
                    for i in elem_dic:
                        list_parameters.append(i)
                    parameters=fetch_parameters(list_parameters,flag_side)
                    return decrypt_www_form_urlencoded(parameters,re_body_text)
                except Exception:
                    return original

            if con_type==5:
                pass
                # TODO for multipart/form-data

        if mxen =='3':
            print("It came here")
            if flag_for_mul_inp==0:
                print colored("Please enter the keys the values of which have to decrypted. Separate by a single space\n","green")
                lis_of_parameters=raw_input()
                list_parameters=lis_of_parameters.split()
                parameters=fetch_parameters(list_parameters,flag_side)
                flag_for_mul_inp=1
                print("This should also be printed only once otherwise go fuck yourself")
            else:
                if flag_side==0:
                    file=open("reqpara.txt","r")
                elif flag_side==1:
                    file=open("respara.txt","r")
                parameters=file.readlines()
                for i in range(0,len(parameters)):
                    parameters[i]=parameters[i].replace("\n","")
                file.close()
            if con_type==1:
                return decrypt_json(parameters,re_body_text)

            if con_type==3:
                return decrypt_xml(parameters,re_body_text)

            if con_type==4:
                return decrypt_www_form_urlencoded(parameters,re_body_text)

            if con_type==5:
                # TODO for urlencod
                pass
        else:
            sys.exit("Please enter valid mxen")



    def save_handler(self, req, req_body, res, res_body):
        self.print_info(req, req_body, res, res_body)



def test(HandlerClass=ProxyRequestHandler, ServerClass=ThreadingHTTPServer, protocol="HTTP/1.1"):
    port = sys.argv[1]
    port=int(port)
    server_address = ('::1', port)
    HandlerClass.protocol_version = protocol
    httpd = ServerClass(server_address, HandlerClass)

    sa = httpd.socket.getsockname()
    print "Serving HTTP Proxy on", sa[0], "port", sa[1], "..."
    httpd.serve_forever()


if __name__ == '__main__':
    #--port --cipMethod --key --cmode --mode_enco --mxen --encrdecr --padding --ivs --segment_size
    global cipMethod
    global keyf
    global cmode
    global ivf
    global ivs
    global segment_size
    global padding
    global mode_enco
    global mode_encod
    global mxen
    global encrdecr
    global iv1
    global flag_for_mul_inp
    global flag_for_url
    global target
    global flag_side
    flag_side=0
    flag_for_url=0
    flag_for_mul_inp=0
    fname = Path("requirements.dat")
    if (fname.exists()):
        print "Inputs"
        fileReq = open("requirements.dat",'r')
        dic = json.loads(fileReq.read())
        cipMethod = dic['cipMethod']
        keyf = dic['keyf']
        cmode = dic['cmode']
        ivf = dic['ivf']
        ivs = str(dic['ivs']) if cmode !='ECB' else None
        segment_size = str(dic['segment_size']) if dic['segment_size']!= None else None
        padding = dic['padding']
        mode_enco = dic['mode_enco']
        mode_encod = "EncDec." + mode_enco + "Enc"
        mxen = str(dic['mxen'])
        encrdecr = str(dic['encrdecr'])
        if encrdecr == 'e':
            encrdecr ='d'
        else:
            encrdecr = 'e'
        print encrdecr
        fileReq.close()
    else:
        dic = {}
        cipMethod = sys.argv[2]
        dic['cipMethod'] =  cipMethod
        keyf = sys.argv[3]
        dic['keyf'] = keyf
        cmode = sys.argv [4]
        dic['cmode'] = cmode
        choose = raw_input ("Want to enter IV(y/n)")
        ivf = raw_input("Enter IV") if cmode != 'ECB' and choose == 'y' else None
        dic['ivf'] = ivf
        ivs = str(sys.argv[9]) if cmode!='ECB' else None #raw_input("Positin of IV: Starting(1) or Ending(2): ")
        dic['ivs'] = ivs
        segment_size = str(sys.argv[10]) if cmode == 'CFB' else None
        dic['segment_size'] = segment_size
        padding = sys.argv[8]  #raw_input("Padding format: Bit, CMS, ZeroLen, Null, ISO, Random,None: ")
        dic['padding'] = padding
        mode_enco = sys.argv[5] #raw_input("Mode of Encoding: Base64/AsciiHex/Bin/Oct/Hex/URL: ")
        dic['mode_enco'] = mode_enco
        mode_encod = "EncDec." + mode_enco + "Enc"
        mxen= str(sys.argv[6]) #raw_input("1)Encrypt/Decrypt Enitre body\n2)Encrypt/Decrypt all values in json/xml\n3)Encrypt/Decrypt multiple parameters\n")
        dic['mxen'] = mxen
        encrdecr = str(sys.argv[7])  # e for encryption, d for decryption
        dic['encrdecr'] = encrdecr
        fileReq = open("requirements.dat",'w+')
        fileReq.write(json.dumps(dic,indent=2))
        fileReq.close()
    test()

