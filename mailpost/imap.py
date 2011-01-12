"""
A package that maps incoming email to HTTP requests
Mailpost version 0.1
(C) 2010 oDesk www.oDesk.com
"""



import imaplib
import email
from cStringIO import StringIO
import re
import pickle
from email.utils import mktime_tz, parsedate_tz, parseaddr

#WARNING: This module is at very early stage of development

#Simplicity and ease of use of the API were chosen over technical correctness
#and performance.
#The goal is to provide an API that is closer to user's perspective than to
#formal standards

#TODO: Lot's of stuff
# Most important
# 1. Dates!!
# 2. Download only headers of a message until body or attachments are
# requested
# 3. Encodings support


class IMAPError(Exception):
    def __init__(self, status, data):
        self.status = status
        self.data = data

    def __str__(self):
        format_data = ', '.join(map(str, self.data))
        return '(Type:%s) %s' % (self.status, format_data)


class Message(object):

    def __init__(self, session, uid):
        self.session = session
        self.uid = uid
        status, data = self.session.uid('FETCH', uid, '(BODY.PEEK[HEADER])')
        if status != 'OK':
            raise IMAPError(status, data)
        self._msg = email.message_from_string(data[0][1])
        self._prepare_header()
        self.downloaded = False

    def _prepare_header(self):
        self.sender   = parseaddr(self._msg['from'])[1]
        self.receiver = parseaddr(self._msg['to'])[1]

    def _prepare_body(self):
        self._text_bodies = []
        self._html_bodies = []
        self._attachments = []
        for part in self._msg.walk():
            filename = part.get_filename()
            ctype = part.get_content_type()
            if filename:
                self._attachments.append((filename, ctype,
                                    StringIO(part.get_payload(decode=True))))
            else:
                if ctype == 'text/plain':
                    self._text_bodies.append(part.get_payload(decode=True))
                elif ctype == 'text/html':
                    self._html_bodies.append(part.get_payload(decode=True))

    @property
    def text_bodies(self):
        self.download()
        return self._text_bodies

    @property
    def html_bodies(self):
        self.download()
        return self._html_bodies

    @property
    def attachments(self):
        self.download()
        return self._attachments

    @property
    def utctimestamp(self):
        datestr = self._msg['date']
        return mktime_tz(parsedate_tz(datestr))

    def __getitem__(self, name):
        return self._msg[name]

    def __contains__(self, name):
        return self.has_key(name)

    def has_key(self, name):
        return self._msg.has_key(name)

    def get(self, name, failobj=None):
        return self._msg.get(name, failobj)

    def __str__(self):
        headers = ("From   : %(from)s\n-----\nTo     : %(to)s\n" +
        "-----\nSubject: %(subject)s\n") % self._msg
        return "%s=====\n%s\n=====\n\n" % (headers, self.body)

    @property
    def body(self):
        return "\n".join(self.text_bodies)

    def add_flag(self, flag):
        status, data = self.session.uid('STORE', self.uid, '+FLAGS', flag)
        if status != 'OK':
            raise Exception('add_flag failed: %s' % data)

    def mark_as_read(self):
        self.add_flag(r'\Seen')

    def copy(self, dest_dir):
        status, data = self.session.copy(self. uid, dest_dir)
        if status != 'OK':
            raise Exception('copy failed: %s' % data)

    def delete(self):
        self.add_flag(r'\Deleted')

    def move(self, dest_dir):
        self.copy(dest_dir)
        self.delete()

    def download(self):
        if self.downloaded:
            return
        status, data = self.session.uid('FETCH', self.uid, '(BODY.PEEK[])')
        if status != 'OK':
            raise IMAPError(status, data)
        self._msg = email.message_from_string(data[0][1])
        self.downloaded = True
        self._prepare_body()

    def pickled(self):
        self.download()
        return pickle.dumps(self._msg)


class MessageList(object):

    def __init__(self, session, query):
        self.session = session
        self.query = query
        self._cache = {}
        self._uids = None

    @property
    def uids(self):
        if self._uids is None:
            charset = None #FIXME
            status, data = self.session.uid('SEARCH', charset, *self.query)
            if status != 'OK':
                raise IMAPError(status, data)
            self._uids = data[0].split()
        return self._uids

    def __len__(self):
        return len(self.uids)

    def __iter__(self):
        for uid in self.uids:
            yield self.get(uid)

    def __getitem__(self, key):
        if not isinstance(key, (slice, int)):
            raise TypeError
        if isinstance(key, slice):
            def msg_gen():
                for uid in self.uids[key]:
                    yield self.get(uid)
            return msg_gen()
        else:
            return self.get(self.uids[key])

    def get(self, uid):
        try:
            return self._cache[uid]
        except KeyError, exc:
            if uid not in self.uids:
                raise exc
            message = Message(self.session, uid)
            self._cache[uid] = message
            return message


class ImapClient(object):

    def __init__(self, host, username, password, port=None, ssl=False):
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.ssl = ssl
        self._connection = None
        self.logged_in = False
        self.mailbox = None
        #self.echo = False

    def connect(self):
        if self.ssl:
            default_port = 993
            cls = imaplib.IMAP4_SSL
        else:
            default_port = 143
            cls = imaplib.IMAP4
        port = self.port or default_port
        self._connection = cls(self.host, port)

    def _be_ready(self):
        if not self.mailbox:
            self.select()

    @property
    def connection(self):
        if not self._connection:
            self.connect()
        return self._connection

    def login(self, username=None, password=None):
        if username is None:
            username = self.username
        if password is None:
            password = self.password

        status, data = self.connection.login(username, password)
        if status != 'OK':
            raise IMAPError(status, data)
        self.logged_in = True

    def select(self, mailbox='INBOX'):
        if not self.logged_in: #TODO: Maybe general 'state' would be better
            self.login(self.username, self.password)
        if self.mailbox:
            self.close()
        status, data = self.connection.select(mailbox)
        if status != 'OK':
            raise IMAPError(status, data)
        self.mailbox = mailbox

    def search(self, *query_args):
        self._be_ready()
        return MessageList(self.connection, query_args)

    def all(self):
        return self.search('ALL')

    def unseen(self):
        return self.search('UNSEEN')

    def nondeleted(self):
        return self.search('(NOT DELETED)')

    def deleted(self):
        return self.search('(DELETED)')

    def close(self):
        if self.mailbox:
            self.mailbox = None
            status, data = self.connection.close()
            if status != 'OK':
                raise IMAPError(status, data)

    def logout(self):
        self.close()
        status, data = self.connection.logout()
        if status != 'BYE':
            raise IMAPError(status, data)
        self._connection = None
        self.logged_in = False

    def list(self, directory='""', pattern='*'):
        self._be_ready()
        status, data = self.connection.list(directory='""', pattern='*')
        if status != 'OK':
            raise IMAPError(status, data)
        return data

    def copy(self, message_set, mailbox):
        self._be_ready()
        status, data = self.connection.copy(message_set, mailbox)
        if status != 'OK':
            raise IMAPError(status, data)
        return data


if __name__ == '__main__':
    from getpass import getpass
    USERNAME = raw_input("Enter your e-mail: ")
    PASSWORD = getpass()
    inbox = ImapClient('imap.gmail.com', USERNAME,
                       PASSWORD, ssl=True)
    print '---- LATEST 10 messages ----'
    for message in inbox.nondeleted()[-10:]:
        print message
    print '---- Directory List ----'
    for directory in inbox.list():
        print directory
    inbox.logout()
