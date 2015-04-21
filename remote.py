import json
import uuid
import sys
# with open('/home/asdf/projects/remote/venv/bin/activate_this.py') as f:
#     code = compile(f.read(), 'activate_this.py', 'exec')
#     exec(code, globals(), locals())
# print(sys.path)
# import twisted
# print(5)
LIBPATHS = [
    '/home/asdf/twisted',
    '/home/asdf/projects/crochet'
]

for libpath in LIBPATHS:
    if not libpath in sys.path:
        sys.path.append(libpath)

import twisted
from crochet import setup
import sublime
setup()
window = sublime.active_window()
view = None
PY2_EXECUTABLE = '/home/asdf/projects/remote/py2venv/bin/python'

REMOTE_VIEWS = []

import os.path
from twisted.internet.protocol import ProcessProtocol
from twisted.internet.defer import Deferred
from crochet import wait_for, run_in_reactor
from twisted.protocols.basic import LineOnlyReceiver


class X(ProcessProtocol):
    delimiter = b'\x00'
    _buffer = b''

    def __init__(self):
        ProcessProtocol.__init__(self)
        self._responses = {}

    def outReceived(self, data):
        self.dataReceived(data)

    def dataReceived(self, data):
        lines  = (self._buffer+data).split(self.delimiter)
        self._buffer = lines.pop(-1)
        for line in lines:
                self.lineReceived(line)

    def errReceived(self, data):
        print('err', data)

    def lineReceived(self, line):
        line = line.decode('utf-8')
        response = json.loads(line)
        resp_data = response['response']
        resp_id = response['id']
        dfd = self._responses.pop(resp_id)
        dfd.callback(resp_data)

    def sendCommand(self, cmd, **connectionOpts):
        dfd = Deferred()
        cmd_id = uuid.uuid4().hex
        self._responses[cmd_id] = dfd
        opts = {
            'cmd': cmd,
            'id': cmd_id,
            'host': 'trewq.pl',
            'user': 'asdf',
            'port': 22
        }
        opts.update(connectionOpts)
        data = json.dumps(opts).encode('utf-8')
        self.sendLine(data)
        return dfd

    def sendLine(self, line):
        return self.transport.writeSequence((line, self.delimiter))



class ProcessCtrl(object):
    transport = None
    proto = None

    @wait_for(1)
    def start(self):
        from twisted.internet import reactor
        self.proto = X()
        self.transport = reactor.spawnProcess(self.proto, PY2_EXECUTABLE, [PY2_EXECUTABLE, '-m', 'child'],
            path=os.path.join(os.path.dirname(__file__), 'child'))


    @wait_for(1)
    def stop(self):
        if self.transport is not None and self.transport.pid:
            self.transport.signalProcess('TERM')
        print('end!')


def writeToView(data, view):
    view.run_command('insert_files', data)

def err(e):
    print(e)

ctrl = ProcessCtrl()
ctrl.start()


def plugin_unloaded():
    ctrl.stop()
    print('unl')

from sublime_plugin import EventListener, TextCommand, WindowCommand



class NoopCommand(TextCommand):
    def run(self, edit):
        pass

from contextlib import contextmanager

@contextmanager
def writable(view):
    view.set_read_only(False)
    try:
        yield
    finally:
        view.set_read_only(True)
import stat

def _files_sort_key(f):
    filename, descr, params = f
    return not stat.S_ISDIR(params['permissions']), filename


class InsertFilesCommand(TextCommand):
    def run(self, edit, **opts):
        files = sorted(opts['files'], key=_files_sort_key)
        start_pos = self.view.size()
        rowcol = self.view.rowcol(start_pos)
        rowcol = float(rowcol[0]), float(rowcol[1])

        with writable(self.view):
            self.view.insert(edit, self.view.size(), '\n')
            self.view.insert(edit, self.view.size(), '\n')
            self.view.insert(edit, self.view.size(), '#' * 80 + '\n')
            self.view.insert(edit, self.view.size(), '\n')
            self.view.insert(edit, self.view.size(), '\t:: {path}\n'.format(path=opts['path']))
            self.view.insert(edit, self.view.size(), '\n')
            for filename, descr, params in files:
                if stat.S_ISDIR(params['permissions']):
                    line = "\t>>>\t{filename}/\n".format(filename=filename)
                else:
                    line = "\t\t{filename}\n".format(filename=filename)
                self.view.insert(edit, self.view.size(), line)


        self.view.show(start_pos + 15)

import bisect
class SelectDirectoryCommand(TextCommand):
    def run(self, edit, **opts):
        all_lines = []
        all_basedir_regions = self.view.find_by_selector('keyword.remote.currentpath')

        for region in self.view.sel():
            for line in self.view.lines(region):

                basedir_ix = bisect.bisect_right(all_basedir_regions, line) - 1
                basedir_region = all_basedir_regions[basedir_ix]
                basedir = self.view.substr(basedir_region)

                decorated_dirname = self.view.substr(line)
                dirname = decorated_dirname.lstrip()[4:-1]
                dirname = os.path.join(basedir, dirname).strip()
                ctrl.proto.sendCommand('directory', args=dirname).addCallback(writeToView, view).addErrback(err)


import tempfile
import os

def editFile(fileData, filename):
    data = fileData['file']
    dirname = tempfile.mkdtemp()
    filepath = os.path.join(dirname, filename)
    with open(filepath, 'w') as f:
        f.write(data)

    fileview = window.open_file(filepath)

class SelectFileCommand(TextCommand):
    def run(self, edit, **opts):
        all_lines = []
        all_basedir_regions = self.view.find_by_selector('keyword.remote.currentpath')

        for region in self.view.sel():
            for line in self.view.lines(region):

                basedir_ix = bisect.bisect_right(all_basedir_regions, line) - 1
                basedir_region = all_basedir_regions[basedir_ix]
                basedir = self.view.substr(basedir_region)

                decorated_filename = self.view.substr(line)
                filename = decorated_filename.lstrip()
                filename = os.path.join(basedir, filename).strip()
                ctrl.proto.sendCommand('file', args=filename).addCallback(editFile, filename).addErrback(err)



class DoConnectCommand(WindowCommand):
    def run(self):
        global view


        for view in window.views():
            if view.name() == '*remote*':
                break
        else:
            view = window.new_file()
            view.set_read_only(True)
            view.set_scratch(True)
            view.set_syntax_file('Packages/Remote/remote.tmLanguage')
            view.set_name('*remote*')
        REMOTE_VIEWS.append(view.buffer_id())
        ctrl.proto.sendCommand('directory', args='/').addCallback(writeToView, view).addErrback(err)