# -*- encoding: UTF-8 -*-
# RFC 2812 Incompleted Implement

import re
import time
import socket
import logging
import functools
from enum import Enum
from tornado.ioloop import IOLoop, PeriodicCallback
from tornado.iostream import IOStream
from .numeric import *

# Initialize logging
logger = logging.getLogger(__name__)
# logger.setLevel(logging.INFO)


def empty_callback(*args, **kw):
    logger.debug('Unimplement callback %s %s' % (args, kw))


# Strip IRC color code
'''
ref: https://github.com/myano/jenni/wiki/IRC-String-Formatting

\x02 	bold
\x1d 	italic text
\x1f 	underlined text
\x16 	swap background and foreground colors ("reverse video")
\x0f 	reset all formatting

\x03<fg>,<bg>text\x03
fg bg :: <= 2 digits number
'''
def strip(msg):
    logger.debug('raw msg: %s', msg)
    tmp = ''
    is_color = 0
    is_fg = is_bg = 0
    for c in msg:
        if c in '\x02\x0f\x16\x1d\x1f':
            continue
        if c == '\x03':
            is_fg = 2
            is_bg = 2
            continue

        if is_fg and c in '0123456789':
            is_fg -= 1
            continue
        elif c == ',' and is_bg == 2:
            is_fg = 0
            is_bg = 2
            continue
        elif is_fg == 0 and is_bg and c in '0123456789':
            is_bg -= 1
            continue
        else:
            is_fg = is_bg = 0
        tmp += c
    logger.debug('msg: %s', tmp)
    return tmp


class IRCMsgType(Enum):
    PING = 0
    NOTICE = 1
    ERROR = 2
    MSG = 3
    UNKNOW = 4


class IRCMsg(object):
    # Prefix
    nick = ''
    user = ''
    host = ''

    # Command
    cmd = ''
    # Middle
    args = []
    # Trailing
    msg = ''


class IRC(object):
    # Private
    _stream = None
    _charset = None
    _ioloop = None
    _timer = None
    _last_pong = None
    _is_reconnect = 0
    _buffers = []
    _send_timer = None

    host = None
    port = None
    nick = None
    chans = []
    chans_ref = {}
    names = {}
    relaybots = []
    delims = [
            ('<' ,'> '),
            ('[' ,'] '),
            ('(' ,') '),
            ('{' ,'} '),
            ]

    # External callbacks
    # Called when you are logined
    login_callback = None
    # Called when you received specified IRC message
    # for usage of event_callback, see `botbox.dispatch`
    event_callback = None

    def __init__(self, host, port, nick,
            relaybots = [],
            charset = 'utf-8',
            ioloop = False):
        logger.info('Connecting to %s:%s', host, port)

        self.host = host
        self.port = port
        self.nick = nick
        self.relaybots = relaybots

        self._charset = charset
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._ioloop = ioloop or IOLoop.instance()
        self._stream = IOStream(sock, io_loop = self._ioloop)
        self._stream.connect((host, port), self._login)

        self._last_pong = time.time()
        self._timer = PeriodicCallback(self._keep_alive,
                60 * 1000, io_loop=self._ioloop)
        self._timer.start()

        self._send_timer = PeriodicCallback(self._sock_send,
                600, io_loop=self._ioloop)
        self._send_timer.start()

    def _sock_send(self):
        if (self._buffers[0:]):
            data = self._buffers.pop(0);
            return self._stream.write(data)

    def _period_send(self, data):
        # Data will be sent in `self._sock_send()`
        self._buffers.append(bytes(data, self._charset))


    def _sock_recv(self):
        def _recv(data):
            msg = data.decode(self._charset, 'ignore')
            msg = msg[:-2]  # strip '\r\n'
            self._recv(msg)

        try:
            self._stream.read_until(b'\r\n', _recv)
        except Exception as err:
            logger.error('Read error: %s', err)
            self._reconnect()


    def _reconnect(self):
        logger.info('Reconnecting...')

        self._is_reconnect = 1

        self._stream.close()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._stream = IOStream(sock, io_loop = self._ioloop)
        self._stream.connect((self.host, self.port), self._login)


    # IRC message parser, return tuple (IRCMsgType, IRCMsg)
    def _parse(self, msg):
        if msg.startswith('PING :'):
            logger.debug('PING')
            return (IRCMsgType.PING, None)
        elif msg.startswith('NOTICE AUTH :'):
            logger.debug('NOTIC AUTH: "%s"')
            return (IRCMsgType.NOTICE, None)
        elif msg.startswith('ERROR :'):
            logger.debug('ERROR: "%s"', msg)
            return (IRCMsgType.ERROR, None)

        try:
            # <message> ::= [':' <prefix> <SPACE> ] <command> <params> <crlf>
            tmp = msg.split(' ', maxsplit = 2)

            if len(tmp) != 3:
                raise Exception('Failed when parsing <prefix> <command> <params>')

            prefix, command, params = tmp
            logger.debug('prefix: "%s", command: "%s", params: "%s"',
                    prefix, command, params)

            # <params> ::= <SPACE> [ ':' <trailing> | <middle> <params> ]
            middle, _, trailing = params.partition(' :')
            if middle.startswith(':'):
                trailing = middle[1:]
                middle = ''
            logger.debug('middle: "%s", trailing: "%s"', middle, trailing)

            if not middle and not trailing:
                middle = trailing = ''
                # raise Exception('No <middle> and <trailing>')

            # <middle> ::= <Any *non-empty* sequence of octets not including SPACE
            #              or NUL or CR or LF, the first of which may not be ':'>
            args = middle.split(' ')
            logger.debug('args: "%s"', args)

            # <prefix> ::= <servername> | <nick> [ '!' <user> ] [ '@' <host> ]
            tmp = prefix
            nick, _, tmp = tmp.partition('!')
            user, _, host = tmp.partition('@')
            logger.debug('nick: "%s", user: "%s", host: "%s"', nick, user, host)

        except Exception as err:
            logger.error('Parsing error: %s', err)
            logger.error('    Message: %s', repr(msg))
            return (IRCMsgType.UNKNOW, None)
        else:
            ircmsg = IRCMsg()
            ircmsg.nick = nick[1:]  # strip ':'
            ircmsg.user = user
            ircmsg.host = host
            ircmsg.cmd = command
            ircmsg.args = args
            ircmsg.msg = trailing
            return (IRCMsgType.MSG, ircmsg)


    # Response server message
    def _resp(self, type_, ircmsg):
        if type_ == IRCMsgType.PING:
            self._pong()
        elif type_ == IRCMsgType.ERROR:
            pass
        elif type_ == IRCMsgType.MSG:
            if ircmsg.cmd == RPL_WELCOME:
                self._on_login(ircmsg.args[0])
            elif ircmsg.cmd == ERR_NICKNAMEINUSE:
                new_nick = ircmsg.args[1] + '_'
                logger.info('Nick already in use, use "%s"', new_nick)
                self._chnick(new_nick)
            elif ircmsg.cmd == 'JOIN':
                chan = ircmsg.args[0] or ircmsg.msg
                if ircmsg.nick == self.nick:
                    self.chans.append(chan)
                    self.names[chan] = set()
                    logger.info('%s has joined %s', self.nick, chan)
                self.names[chan].add(ircmsg.nick)
            elif ircmsg.cmd == 'PART':
                chan = ircmsg.args[0]
                try:
                    self.names[chan].remove(ircmsg.nick)
                except KeyError as err:
                    logger.error('KeyError: %s', err)
                    logger.error('%s %s %s %s %s %s', ircmsg.nick, ircmsg.user,
                            ircmsg.host, ircmsg.cmd, ircmsg.args, ircmsg.msg)
                if ircmsg.nick == self.nick:
                    self.chans.remove(chan)
                    self.names[chan].clear()
                    logger.info('%s has left %s', self.nick, ircmsg.args[0])
            elif ircmsg.cmd == 'NICK':
                new_nick, old_nick = ircmsg.msg, ircmsg.nick
                for chan in self.chans:
                    if old_nick in self.names[chan]:
                        self.names[chan].remove(old_nick)
                        self.names[chan].add(new_nick)
                if old_nick == self.nick:
                    self.nick = old_nick
                    logger.info('%s is now known as %s', old_nick, new_nick)
            elif ircmsg.cmd == 'QUIT':
                nick = ircmsg.nick
                for chan in self.chans:
                    if nick in self.names[chan]:
                        self.names[chan].remove(nick)
            elif ircmsg.cmd == RPL_NAMREPLY:
                chan = ircmsg.args[2]
                names_list = [x[1:] if x[0] in ['@', '+'] else x
                        for x in ircmsg.msg.split(' ')]
                self.names[chan].update(names_list)
                logger.debug('NAMES: %s' % names_list)


    def _dispatch(self, type_, ircmsg):
        if type_ != IRCMsgType.MSG:
             return

        # Error message
        if ircmsg.cmd[0] in ['4', '5']:
            logger.warn('Error message: %s', ircmsg.msg)
        elif ircmsg.cmd in ['JOIN', 'PART']:
            nick, chan = ircmsg.nick, ircmsg.args[0] or ircmsg.msg
            self.event_callback(ircmsg.cmd, chan, nick)
        elif ircmsg.cmd == 'QUIT':
            nick, reason = ircmsg.nick, ircmsg.msg
            for chan in self.chans:
                if nick in self.names[chan]:
                    self.event_callback(ircmsg.cmd, chan, nick, reason)
        elif ircmsg.cmd == 'NICK':
            new_nick, old_nick = ircmsg.msg, ircmsg.nick
            for chan in self.chans:
                if old_nick in self.names[chan]:
                    self.event_callback(ircmsg.cmd, chan, old_nick, new_nick)
        elif ircmsg.cmd in ['PRIVMSG', 'NOTICE']:
            if ircmsg.msg.startswith('\x01ACTION '):
                msg = ircmsg.msg[len('\x01ACTION '):-1]
                cmd = 'ACTION'
            else:
                msg, cmd = ircmsg.msg, ircmsg.cmd
            nick, target = ircmsg.nick, ircmsg.args[0]
            self.event_callback(cmd, target, nick, msg)

            # LABOTS_MSG = ACTION or PRIVMSG or NOTICE
            # And it will:
            # - Strip IRC color codes
            # - Replace relaybot's nick with human's nick
            bot = ''
            msg = strip(msg)
            if nick in self.relaybots:
                for d in self.delims:
                    if msg.startswith(d[0]) and msg.find(d[1]) != -1:
                        bot = nick
                        nick = msg[len(d[0]):msg.find(d[1])]
                        msg = msg[msg.find(d[1])+len(d[1]):]
                        break
            self.event_callback('LABOTS_MSG', target, bot, nick, msg)

            # LABOTS_MENTION_MSG = LABOTS_MSG + labots's nick is mentioned at
            # the head of message
            words = msg.split(' ', maxsplit = 1)
            if words[0] in [ self.nick + x for x in ['', ':', ','] ]:
                if words[1:]:
                    msg = words[1]
                    self.event_callback('LABOTS_MENTION_MSG', target, bot, nick, msg)


    def _keep_alive(self):
        # Ping time out
        if time.time() - self._last_pong > 360:
            logger.error('Ping time out')

            self._reconnect()
            self._last_pong = time.time()


    def _recv(self, msg):
        if msg:
            type_, ircmsg = self._parse(msg)
            self._dispatch(type_, ircmsg)
            self._resp(type_, ircmsg)

        self._sock_recv()


    def _chnick(self, nick):
        self._period_send('NICK %s\r\n' % nick)


    def _on_login(self, nick):
        logger.info('You are logined as %s', nick)

        self.nick = nick
        chans = self.chans

        if not self._is_reconnect:
            self.login_callback()

        self.chans = []
        [self.join(chan, force = True) for chan in chans]


    def _login(self):
        logger.info('Try to login as "%s"', self.nick)

        self._chnick(self.nick)
        self._period_send('USER %s %s %s %s\r\n' % (self.nick, 'labots',
            'localhost', 'https://github.com/SilverRainZ/labots'))

        self._sock_recv()


    def _pong(self):
        logger.debug('Pong!')

        self._last_pong = time.time()
        self._period_send('PONG :labots!\n')


    def set_callback(self,
            login_callback = empty_callback,
            event_callback = empty_callback):
        self.login_callback = login_callback
        self.event_callback = event_callback


    def join(self, chan, force = False):
        if chan[0] not in ['#', '&']:
            return

        if not force:
            if chan in self.chans_ref:
                self.chans_ref[chan] += 1
                return
            self.chans_ref[chan] = 1

        logger.debug('Try to join %s', chan)
        self._period_send('JOIN %s\r\n' % chan)


    def part(self, chan):
        if chan[0] not in ['#', '&']:
            return
        if chan not in self.chans_ref:
            return

        if self.chans_ref[chan] != 1:
            self.chans_ref[chan] -= 1
            return

        self.chans_ref.pop(chan, None)

        logger.debug('Try to part %s', chan)
        self._period_send('PART %s\r\n' % chan)


    # recv_msg: Whether receive the message you sent
    def send(self, target, msg, recv_msg = True):
        lines = msg.split('\n')
        for line in lines:
            self._period_send('PRIVMSG %s :%s\r\n' % (target, line))
            # You will recv the message you sent
            if recv_msg:
                self.event_callback('PRIVMSG', target, self.nick, line)


    def action(self, target, msg):
        self._period_send('PRIVMSG %s :\1ACTION %s\1\r\n')
        # You will recv the message you sent
        self.event_callback('ACTION', target, self.nick, msg)


    def topic(self, chan, topic):
        self._period_send('TOPIC %s :%s\r\n' % (chan, topic))

    def kick(self, chan, nick, reason):
        self._period_send('KICK %s %s :%s\r\n' % (chan, nick, topic))

    def quit(self, reason = '食饭'):
        self._period_send('QUIT :%s\r\n' % reason)


    def stop(self):
        logger.info('Stop')
        self.quit()
        self._stream.close()

if __name__ == '__main__':
    logging.basicConfig(format = '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s - %(message)s')
    irc = IRC('irc.freenode.net', 6667, 'labots')
    irc.set_callback()
    try:
        IOLoop.instance().start()
    except KeyboardInterrupt:
        irc.stop()
        IOLoop.instance().stop()
