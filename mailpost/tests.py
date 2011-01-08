"""
A package that maps incoming email to HTTP requests
Mailpost version 0.1
(C) 2010 oDesk www.oDesk.com
"""



import re
import urllib
import urllib2
import os
import imaplib
import email
from cStringIO import StringIO
from datetime import datetime

import unittest
from mock import Mock, patch

from mailpost.fnmatch import fnmatch, fnmatchcase, translate
from mailpost.imap import ImapClient, Message
from mailpost.handler import Handler, Mapper, Config


class TestFnmatch(unittest.TestCase):

    def check_match(self, filename, pattern, should_match=1):
        if should_match:
            self.assert_(fnmatch(filename, pattern),
                         "expected %r to match pattern %r"
                         % (filename, pattern))
        else:
            self.assert_(not fnmatch(filename, pattern),
                         "expected %r not to match pattern %r"
                         % (filename, pattern))

    def check_translate(self, sample, pattern, should_match=1):
        if should_match:
            self.assert_(translate(pattern) == sample + '\Z(?ms)',
                         "expected %r to match pattern %r"
                         % (sample, pattern))
        else:
            self.assert_(not translate(pattern) == sample + '\Z(?ms)',
                         "expected %r not to match pattern %r"
                         % (sample, pattern))

    def test_fnmatch(self):
        check = self.check_match
        check('abc', 'abc')
        check('abc', '?*?')
        check('abc', '???*')
        check('abc', '*???')
        check('abc', '???')
        check('abc', '*')
        check('abc', 'ab[cd]')
        check('abc', 'ab[!de]')
        check('abc', 'ab[de]', 0)
        check('a', '??', 0)
        check('a', 'b', 0)

        # these test that '\' is handled correctly in character sets;
        check('\\', r'[\]')
        check('a', r'[!\]')
        check('\\', r'[!\]', 0)

        # these test that escaping works
        check('[test]', r'\[test\]')
        check('[$%^est]', r'\[\$\%\^est\]')
        check('a*c', '?\*?')
        check('a?bc', '?\??*')
        check('ab??c', '*\?\??')
        check('*abc', '\**')
        check('abd', 'ab[de]')
        check('abc', 'a\[bc', 0)
        #when we escape only ], it handled as usual in glob - [] rule
        check('abd', 'ab[de\]', 1)
        check('abda', 'ab[de\]?', 1)
        #when we escape only [ - it works as escaped for both
        check('ab[de]', 'ab\[de]', 1)
        check('abd', 'ab\[de]', 0)

        #check some rules directly using fnmatch.translate
        check = self.check_translate
        check('\[\$\%\^est\]', r'\[\$\%\^est\]')
        check('\*\*', '\*\*')
        check('\*\[[*]', '\*[*]', 0)


string_message = '''from:TESTserveradministrator@gmail.com;
to:TESTlillianc@gmail.com;
subject:[AVAILABLE FOR TRANSLATION] A task in our server
Date: %s
Message-ID: %s
project 'New project;
 Test - test2' is now available to review
=====
A task in our server project 'New project;
 Test - test2' is now available to review
=====;
'''

class TestMessage(object):
    def __init__(self, uid, date, flags=(r'\Seen',)):
        self.uid   = uid
        self.date  = date
        self.flags = set(flags)

    def as_string(self):
        global string_message
        return string_message % (self.date, self.uid)


class MockIMAP(Mock):
    def __init__(self):
        Mock.__init__(self)

        self.mailboxes = {}
        self.mailboxes['inbox']   = [TestMessage(str(i), datetime(2010, 12, i), flags=[r'\Seen'] if i < 21 else []) 
                                     for i in range(6, 31)]
        self.mailboxes['starred'] = [TestMessage(str(i), datetime(2010, 5, (i-30))) 
                                     for i in range(31, 51)]
        self.mailboxes['archive'] = []
        self.logged_in = False
        self.selected = None
        self.uidnext = 51

    def login(self, username, password):
        self.logged_in = True
        return ('OK', None)

    def logout(self):
        if not self.logged_in:
            return 'NOT', 'not logged in'
        self.logged_in = False
        return ('OK', None)

    def select(self, mailbox):
        mailbox = mailbox.lower()
        if self.selected:
            return 'NOT', '%s already selected must close() first' % self.selected
        if mailbox not in self.mailboxes:
            return 'BAD', 'mailbox %s not available' % mailbox
        self.selected = self.mailboxes[mailbox]
        return ('OK', None)

    def close(self):
        if not self.selected:
            return 'NOT', 'nothing to close'
        self.selected = None
        return ('OK', None)

    def get_msg(self, uid):
        return [m for m in self.selected if m.uid == uid]

    def uid(self, *args, **kwargs):
        if kwargs:
            raise Exception('positional args not tested (TODO)')

        if not self.logged_in:
            return 'BAD', 'not logged in'

        if not self.selected:
            return 'BAD', 'mailbox not selected'

        command = args[0]
        if command == 'FETCH':
            msg = self.get_msg(uid=args[1])
            if msg:
                return 'OK', [[None, msg[0].as_string()]]
            return ('NO', 'message not found: uid:%s' % args[1])

        elif command == 'SEARCH':
            if list(args[2:]) == ['ALL']:
                return 'OK', [' '.join([str(msg.uid) for msg in self.selected])]
            elif list(args[2:]) == ['UNSEEN']:
                return 'OK', [' '.join([str(msg.uid) for msg in self.selected
                              if r'\Seen' not in msg.flags])]
            elif args[2] == 'BEFORE':
                dt = datetime.strptime(args[3], '%d-%b-%Y')
                return 'OK', [' '.join([str(msg.uid) for msg in self.selected
                              if msg.date < dt])]
            elif args[2] == 'SINCE':
                dt = datetime.strptime(args[3], '%d-%b-%Y')
                return 'OK', [' '.join([str(msg.uid) for msg in self.selected
                              if msg.date >= dt])]
            else:
                return 'BAD', 'unexpected query %s' % args[2:]

        elif command == 'STORE':
            msg = self.get_msg(uid=args[1])
            if not msg:
                return ('NOT', 'message not found')
            msg = msg[0]
            if args[2] == '+FLAGS':
                msg.flags.update(args[3:])
            else:
                msg.flags = set(args[3:])
            if r'\Deleted' in msg.flags:
                self.selected.remove(msg)
                self.mailboxes.setdefault('(deleted)', []).append(msg)
            return ('OK', None)

        else:
            raise Exception('can not test command %s' % command)

    def copy(self, uid, dest_dir):
        msg = self.get_msg(uid)
        if not msg:
            return ('NO', 'message not found')
        if dest_dir not in self.mailboxes:
            return ('BAD', 'mailbox %s does not exist' % dest_dir)
        msg = msg[0]
        new_msg = TestMessage(uid=str(self.uidnext), date=msg.date, flags=msg.flags)
        self.mailboxes[dest_dir].append(new_msg)
        self.uidnext += 1
        return ('OK', None)

    def list():
        raise NotImplementedError()


logged_requests = {}
def patched_urlopen(request, *args, **kwargs):
    global logged_requests
    logged_requests.setdefault(request.get_full_url(), []).append(list(request.get_data()))
    request.read = lambda: 'OK'
    return request


@patch('urllib2.urlopen', patched_urlopen)
@patch.object(ImapClient, 'connection', MockIMAP())
class TestMailPost(unittest.TestCase):

    def __init__(self, *args, **kwargs):

        super(TestMailPost, self).__init__(*args, **kwargs)

        sample_rules = [
                {
                    'url': '/upload_unseen_email/',
                    'conditions': {
                        'subject': ['*AVAILABLE FOR TRANSLATION*', ],
                    },
                    'add_params': {'message_type':'test'},
                    'actions': ['mark_as_read'],
                    'query': ['UNSEEN'],
                    'raw': 'true',
                },
                {
                    'url': '/upload_email/',
                    'query': ['BEFORE', '10-Dec-2010'],
                    'msg_params': ['html_bodies'],
                },
                {
                    'url': '/upload_email/',
                    'query': ['BEFORE', '11-May-2010'],
                    'mailbox': 'starred',
                    'msg_params': ['html_bodies'],
                },
            ]

        archive_life = datetime.now() - datetime(2010, 12, 16)
        archive_life = str(archive_life).split(',')[0]
        sample_config = {
            'backend': 'imap',
            'host': 'imap.gmail.com',
            'ssl': 'true',
            'username': 'clientg.test@gmail.com',
            'password': 'ClientGoogle',
            'base_url': 'http://localhost:8000/',
            'archive': 'archive',
            'archive_life': archive_life,
            'rules': sample_rules,
        }

        self.sample_config = Config(config = sample_config)

    def test_mapper_current_workflow(self, *args, **kwargs):
        mockclient = ImapClient("", "", "")
        mockclient.select()

        mapper = Mapper(mockclient, self.sample_config)
        matches = [(rule['mailbox'], msg) for (msg, rule) in mapper.get_messages(self.sample_config['rules'])]
        self.assert_(len(matches) == len(set(matches)), len(set(matches)))
        boxes = {}
        for box, msg in matches:
            boxes.setdefault(box, []).append(msg)
        self.assert_(len(boxes.keys()) == 2, len(boxes.keys()))
        self.assert_(len(boxes['INBOX']) == 14, len(boxes['INBOX']))
        self.assert_(len(boxes['starred']) == 10, len(boxes['starred']))

        def mailbox_len(mailbox):
            mockclient.select(mailbox)
            return len(mockclient.search('ALL'))

        for _, res in mapper.process(self.sample_config['rules']):
            self.assert_(res=='OK', res)

        self.assert_(mailbox_len('INBOX')==14,        mailbox_len('INBOX'))
        self.assert_(mailbox_len('starred')==10,      mailbox_len('starred'))
        self.assert_(mailbox_len('archive')==5,       mailbox_len('(deleted)'))
        self.assert_(mailbox_len('(deleted)')==11+26, mailbox_len('(deleted)'))

        self.assert_(len(logged_requests.keys())==2, logged_requests.keys())
        for full_url, params_list in logged_requests.items():
            if 'unseen' in full_url:
                for params in params_list:
                    self.assert_(any(['name="raw_message"' in item for item in params]), 'raw_message')
                    self.assert_(not any(['name="html_bodies"' in item for item in params]), 'not html_bodies')
            else:
                for params in params_list:
                    self.assert_(not any(['name="raw_message"' in item for item in params]), 'raw_message')
                    param = [item for item in params if 'name="html_bodies"' in item]
                    self.assert_(len(param) == 1, 'html_bodies')
                    param = param[0]
                    import pickle
                    pickle_string = pickle.dumps([])
                    self.assert_(pickle_string in param, 'pickle.dumps([])')



if __name__ == '__main__':
    unittest.main()
