#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import time
import json
import socket
import logging
import requests
import threading
import posixpath
import mimetypes
import http.server
import socketserver
import urllib.parse

import tgcli
from itsdangerous import URLSafeTimedSerializer

RE_INVALID = re.compile("[\000-\037\t\r\x0b\x0c\ufeff]")

logging.basicConfig(stream=sys.stderr, format='%(asctime)s [%(name)s:%(levelname)s] %(message)s', level=logging.DEBUG if sys.argv[-1] == '-v' else logging.INFO)

logger_botapi = logging.getLogger('botapi')
logger_http = logging.getLogger('http')

class AttrDict(dict):

    def __init__(self, *args, **kwargs):
        super(AttrDict, self).__init__(*args, **kwargs)
        self.__dict__ = self

# API bot

HSession = requests.Session()


class BotAPIFailed(Exception):
    pass


def bot_api(method, **params):
    for att in range(3):
        try:
            req = HSession.get(('https://api.telegram.org/bot%s/' %
                                CFG.apitoken) + method, params=params, timeout=45)
            retjson = req.content
            ret = json.loads(retjson.decode('utf-8'))
            break
        except Exception as ex:
            if att < 1:
                time.sleep((att + 1) * 2)
            else:
                raise ex
    if not ret['ok']:
        raise BotAPIFailed(repr(ret))
    return ret['result']


def getupdates():
    global STATE
    while 1:
        try:
            updates = bot_api('getUpdates', offset=STATE['offset'], timeout=10)
        except Exception as ex:
            logger_botapi.exception('Get updates failed.')
            continue
        if updates:
            logger_botapi.debug('Messages coming.')
            STATE['offset'] = updates[-1]["update_id"] + 1
            for upd in updates:
                processmsg(upd)
        time.sleep(.2)


def processmsg(d):
    logger_botapi.debug('Msg arrived: %r' % d)
    if 'message' in d:
        msg = d['message']
        if msg['chat']['type'] == 'private' and msg.get('text', '').startswith('/t'):
            bot_api('sendMessage', chat_id=msg['chat']['id'],
                    text=CFG.url + get_token(msg['from']['id']))
            logger_botapi.info('Sent a token to %s' % msg['from'])

# Cli bot


def get_members():
    global CFG
    # To ensure the id is valid
    TGCLI.cmd_dialog_list()
    peername = '%s#id%d' % (CFG.grouptype, CFG.groupid)
    STATE.members = {}
    if CFG.grouptype == 'channel':
        items = TGCLI.cmd_channel_get_members(peername, 100)
        for item in items:
            STATE.members[str(item['peer_id'])] = item
        dcount = 100
        while items:
            items = TGCLI.cmd_channel_get_members(peername, 100, dcount)
            for item in items:
                STATE.members[str(item['peer_id'])] = item
            dcount += 100
        STATE.title = TGCLI.cmd_channel_info(peername)['title']
    else:
        obj = TGCLI.cmd_chat_info(peername)
        STATE.title = obj['title']
        items = obj['members']
        for item in items:
            STATE.members[str(item['peer_id'])] = item
    logging.info('Original title is: ' + STATE.title)


def handle_update(obj):
    global STATE
    try:
        if (obj.get('event') == 'message' and obj['to']['peer_id'] == CFG.groupid and obj['to']['peer_type'] == CFG.grouptype):
            STATE.members[str(obj['from']['peer_id'])] = obj['from']
            STATE.title = obj['to']['title']
    except Exception:
        logging.exception("can't handle message event")

# HTTP Server


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):

    def server_bind(self, *args, **kwargs):
        super().server_bind(*args, **kwargs)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


class HTTPHandler(http.server.BaseHTTPRequestHandler):

    def send_response(self, code, message=None):
        self.send_response_only(code, message)
        self.send_header('Server', self.version_string())
        self.send_header('Date', self.date_time_string())

    def log_message(self, format, *args):
        logger_http.info('%s - - [%s] %s "%s" "%s"' % (
            self.headers.get('X-Forwarded-For', self.address_string()),
            self.log_date_time_string(), format % args, self.headers.get('Referer', '-'), self.headers.get('User-Agent', '-')))

    def log_date_time_string(self):
        """Return the current time formatted for logging."""
        lt = time.localtime(time.time())
        s = time.strftime('%d/%%3s/%Y:%H:%M:%S %z', lt) % self.monthname[lt[1]]
        return s

    def title_api(self, path):
        path = urllib.parse.unquote_plus(path, errors='ignore')
        qs = path.split('?', 1)
        path = qs[0].split('#', 1)[0].rstrip()
        if path == '/title':
            if len(qs) > 1:
                query = urllib.parse.parse_qs(qs[1])
            else:
                query = {}
            if 't' in query:
                if 'n' in query:
                    newtitle = query['n'][0]
                    code, ret = change_title(query['t'][0], newtitle)
                    if code == 200:
                        ret['title'] = newtitle
                        ret['prefix'] = CFG.prefix
                    elif code != 403:
                        ret['title'] = cut_title(STATE.title)
                        ret['prefix'] = CFG.prefix
                    return code, json.dumps(ret)
                else:
                    uid = verify_token(query['t'][0])
                    if uid:
                        return 200, json.dumps({'title': cut_title(STATE.title), 'prefix': CFG.prefix})
                    else:
                        return 403, json.dumps({'error': 'invalid token'})
            else:
                return 403, json.dumps({'error': 'token not specified'})
        elif path == '/members':
            return 200, json.dumps(STATE.members)
        else:
            return None, None

    def do_GET(self):
        code, text = self.title_api(self.path)
        if code is None:
            fn = self.translate_path(CFG.staticdir, self.path, 'index.html')
            if os.path.isfile(fn):
                code, text = 200, open(fn, 'rb').read()
                ctype = mimetypes.guess_type(fn)[0]
            else:
                code, text = 404, b'404 Not Found'
                ctype = 'text/plain'
        else:
            text = text.encode('utf-8')
            ctype = 'application/json'
        self.send_response(code)
        length = len(text)
        self.log_request(code, length)
        self.send_header('Content-Length', length)
        self.send_header('Content-Type', ctype)
        self.end_headers()
        self.wfile.write(text)

    def translate_path(self, base, path, index=''):
        """Translate a /-separated PATH to the local filename syntax.

        Components that mean special things to the local file system
        (e.g. drive or directory names) are ignored.  (XXX They should
        probably be diagnosed.)

        """
        # abandon query parameters
        path = path.split('?',1)[0]
        path = path.split('#',1)[0]
        # Don't forget explicit trailing slash when normalizing. Issue17324
        trailing_slash = path.rstrip().endswith('/')
        try:
            path = urllib.parse.unquote(path, errors='surrogatepass')
        except UnicodeDecodeError:
            path = urllib.parse.unquote(path)
        path = posixpath.normpath(path)
        words = path.split('/')
        words = filter(None, words)
        path = base
        for word in words:
            drive, word = os.path.splitdrive(word)
            head, word = os.path.split(word)
            if word in (os.curdir, os.pardir): continue
            path = os.path.join(path, word)
        if trailing_slash:
            path += '/' + index
        return path

# Processing


def token_gc():
    for uid, gentime in tuple(STATE.tokens.items()):
        if time.time() - gentime > CFG.tokenexpire:
            del STATE.tokens[str(uid)]


def get_token(uid):
    serializer = URLSafeTimedSerializer(CFG.secretkey, 'Orz')
    STATE.tokens[str(uid)] = time.time()
    return serializer.dumps(uid)


def verify_token(token):
    serializer = URLSafeTimedSerializer(CFG.secretkey, 'Orz')
    try:
        uid = serializer.loads(token, max_age=CFG.tokenexpire)
        if str(uid) not in STATE.members:
            return False
        if time.time() - STATE.tokens[str(uid)] > CFG.tokenexpire:
            return False
    except Exception:
        return False
    return uid


def cut_title(title):
    return title[len(CFG.prefix):]


def change_title(token, title):
    uid = verify_token(token)
    if uid is False:
        return 403, {'error': 'invalid token'}
    title = RE_INVALID.sub('', title).replace('\n', ' ')
    if len(CFG.prefix + title) > 255:
        return 400, {'error': 'title too long'}
    ret = TGCLI.cmd_rename_channel('%s#id%d' % (CFG.grouptype, CFG.groupid),
                                   CFG.prefix + title)
    if ret['result'] == 'SUCCESS':
        user = STATE.members[str(uid)]
        uname = user.get('username')
        if uname:
            bot_api('sendMessage', chat_id=CFG.apigroupid,
                    text='@%s 修改了群组名称。' % uname)
        else:
            uname = user.get('first_name', '')
            if 'last_name' in user:
                uname += ' ' + user['last_name']
            bot_api('sendMessage', chat_id=CFG.apigroupid,
                    text='%s 修改了群组名称。' % uname)
        del STATE.tokens[str(uid)]
        STATE.title = CFG.prefix + title
        logging.info('@%s changed title to %s' % (uname, STATE.title))
        return 200, ret
    else:
        return 406, ret


def load_config():
    cfg = AttrDict(json.load(open('config.json', encoding='utf-8')))
    if os.path.isfile('state.json'):
        state = AttrDict(json.load(open('state.json', encoding='utf-8')))
    else:
        state = AttrDict({'offset': 0, 'members': {}, 'tokens': {}})
    return cfg, state


def save_config():
    json.dump(STATE, open('state.json', 'w'), sort_keys=True, indent=1)


def run(server_class=ThreadingHTTPServer, handler_class=HTTPHandler):
    server_address = (CFG.serverip, CFG.serverport)
    httpd = server_class(server_address, handler_class)
    httpd.serve_forever()

if __name__ == '__main__':
    CFG, STATE = load_config()
    TGCLI = tgcli.TelegramCliInterface(CFG.tgclibin)
    TGCLI.ready.wait()
    TGCLI.on_json = handle_update
    try:
        if not STATE.members:
            get_members()
        token_gc()

        apithr = threading.Thread(target=getupdates)
        apithr.daemon = True
        apithr.start()

        run()
    finally:
        save_config()
        TGCLI.close()
