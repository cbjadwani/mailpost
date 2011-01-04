"""
A package that maps incoming email to HTTP requests
Mailpost version 0.1
(C) 2010 oDesk www.oDesk.com
"""

import re
import urllib
import urllib2
import os
from imap import ImapClient
from poster.encode import multipart_encode, MultipartParam
from poster.streaminghttp import register_openers

from mailpost import fnmatch
from mailpost import auth

#TODO: Everything.

# This module is in 'works for now' stage
# Many features are missing and not all of existing features
# were decently tested
# You'd better stick to standard options for now

DEFAULT_RULE = {
    #Request params
    'method': 'post', #TODO: method is ignored for now
    'conditions': {},
    'syntax': 'glob',
    #Send message unparsed
    'raw': False,
    #Which message params to include in request
    'msg_params': ['from', 'sender', 'to', 'receiver', 'subject', 'body',
                   'date', 'Message-ID'],
    #Additional request params
    'add_params': {},
    'send_files': True,
    #Backend-specific actions
    'actions': [],
    'query': ['ALL'],
}


class ConfigurationError(Exception):
    pass


class Mapper(object):

    def __init__(self, client, base_url=None):
        self.client = client
        self.base_url = base_url

    def map(self, rule):
        url = rule['url']
        if self.base_url:
            url = self.base_url.rstrip('/') + '/' + url.lstrip('/')
        match_func = fnmatch.fnmatch
        if rule['syntax'] == 'regexp':
            mathc_func = re.match
        query_args = rule['query']
        msg_list = self.client.search(*query_args)

        for message in msg_list:
            match = True
            for key, pattern in rule['conditions'].items():
                value = message.get(key, None)
                if not value:
                    value = getattr(message, key, None)
                if not value:
                    match = False
                    break
                if type(pattern) in [list, tuple]:
                    match &= any([match_func(value, item) for item in pattern])
                elif isinstance(pattern, str):
                    match &= match_func(value, pattern)
                else:
                    raise ConfigurationError(\
                                "Pattern should be string or list, not %s" %\
                                             type(pattern))
            if match:
                yield url, message


    def get_messages(self, rules):
        for rule in rules:
            for url, message in self.map(rule):
                yield url, message, rule


    def process(self, rules):
        """
        Inbox is expected to be a list of imap.Message objects.
        Although any list of mapping objects is accepted, provided
        that objects support methods enlisted in 'actions' option
        """

        # Poster: Register the streaming http handlers with urllib2
        register_openers()

        for url, message, options in self.get_messages(rules):
            for action in options['actions']:
                getattr(message, action)()
            files = []
            if options['send_files']:
                for num, attachment in enumerate(message.attachments):
                    filename, ctype, fileobj = attachment
                    file_param = MultipartParam('attachment[%d]' % num,
                                                filename=filename,
                                                filetype=ctype,
                                                fileobj=fileobj)
                    files.append(file_param)
            data = {}
            for name in options['msg_params']:
                part = message.get(name, None)
                if not part:
                    part = getattr(message, name, None)
                if part: #TODO: maybe we should raise an exception
                         #if there's no part
                    data[name] = part
            data.update(options['add_params'])
            data = MultipartParam.from_params(data)
            data += files
            datagen, headers = multipart_encode(data)
            request = urllib2.Request(url, datagen, headers)
            if options.get('auth', None):
                cj, urlopener = auth.authenticate(options['auth'], request, 
                                                  self.base_url)
            try:
                result = urllib2.urlopen(request).read()
            except urllib2.URLError, e:
                result = e
                #continue    # TODO Log error and proceed.
            yield url, result


class Config(dict):
    def __init__(self, config=None, config_file=None, fileformat=None):
        super(Config, self).__init__()

        if not config:
            if not config_file:
                raise ValueError(\
                            "Either config or config_file must be specified")
            if not fileformat:
                fileformat = os.path.splitext(config_file)[1][1:]
            if fileformat in ['yml', 'yaml']:
                import yaml
                config = yaml.load(open(config_file, 'r'))
            else:
                raise ConfigurationError(
                        "Unknown config file format %s" % fileformat)

        def opt(key, default=None, required=False, vals=None, type_='scalar'):
            if not required and vals is not None:
                default = vals[0]
                if type_ == 'list':
                    default = [default]
            val = config.get(key, default)
            if required and (val is None):
                raise ConfigurationError(
                        "'%s' configuration option is required" % key)
            if vals is not None:
                if type_ == 'list':
                    if not all([v in vals for v in val]):
                        raise ConfigurationError(
                                "Setting not supported: '%s'='%s'" % (key, val))
                if val not in vals:
                    raise ConfigurationError(
                            "Setting not supported: '%s'='%s'" % (key, val))
            return val

        self['backend']   = opt('backend', required=True, vals=['imap'])
        self['host']      = opt('host', required=True)
        self['username']  = opt('username', required=True)
        self['password']  = opt('password', required=True)
        self['port']      = opt('port', default=None)
        self['ssl']       = opt('ssl', default=False)
        self['mailboxes'] = opt('inboxes', ['INBOX'], type_='list')
        self['base_url']  = opt('base_url', None)

        config_rules = opt('rules', required=True)
        self['rules'] = []
        for config_rule in config_rules:
            rule = DEFAULT_RULE.copy()
            if 'url' not in config_rule:
                raise ConfigurationError('URL is required for rules')
            rule.update(config_rule)
            self['rules'].append(rule)


class Handler(object):

    def __init__(self, config=None, config_file=None, fileformat=None):
        """
        Either `config` or `config_file` must be pspecified
        `config` is a mapping that contains configuration options
        `config_file` is a name of configuration file
        If `fileformat` is absent, it will try to guess
        Possible values for `fileformat` are: 'yml'('yaml')
        """
        self.config = Config(config, config_file, fileformat)

    def load_backend(self):
        if self.config['backend'] == 'imap':
            client = ImapClient(self.config['host'],
                                self.config['username'],
                                self.config['password'],
                                self.config['port'],
                                self.config['ssl'])
            self.client = client

            self.base_url = self.config['base_url']
            self.rules = self.config['rules']
        else:
            raise ConfigurationError("Backend '%s' is not supported" %\
                                     self.config['backend'])

    def process(self):
        self.load_backend()
        mapper = Mapper(self.client, self.base_url)
        for url, result in mapper.process(self.rules):
            yield url, result


if __name__ == '__main__':
    sample_rules = [
        {
            'url': 'http://localhost:8000/translation_mail_test/',
            'conditions': {
                'sender': ['*@gmail.com', '*@odesk.com', '*@google.com'],
            },
            'add_params': {'message_type':'test'},
            'actions': ['mark_as_read'],
            'query': ['SUBJECT', 'translation', 'SINCE', '15-Dec-2010']
        },
        { #"Catch all" rule
            'url': 'http://localhost:8000/mail_test/',
            'conditions': {
                'subject': '*task*',
            },
            'query': ['BEFORE', '1-Nov-2010'],
        },
    ]

    sample_config = {
        'backend': 'imap',
        'host': 'imap.gmail.com',
        'ssl': 'true',
        'username': 'clientg.test@gmail.com',
        'password': 'ClientGoogle',
        'rules': sample_rules,
    }

    handler = Handler(config=sample_config)
    for url, result in handler.process():
        print "URL:", url
        print "result:", result
        print
