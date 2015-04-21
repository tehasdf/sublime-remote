import json

from collections import defaultdict
from twisted.conch.ssh import transport, userauth, connection, common, keys, channel, session
from twisted.conch import error
from twisted.internet import defer, protocol, reactor, stdio
from twisted.protocols.basic import LineReceiver
from twisted.python import log
import struct, sys, getpass, os

USER = 'asdf'
HOST = 'trewq.pl'

PRIVKEY = keys.Key.fromFile(os.path.expanduser('~/.ssh/id_rsa'))
PUBKEY = keys.Key.fromFile(os.path.expanduser('~/.ssh/id_rsa.pub'))


class ClientTransport(transport.SSHClientTransport):
    def __init__(self, user, conn):
        self._user = user
        self.conn = conn

    def verifyHostKey(self, pubKey, fingerprint):
        return defer.succeed(1)

    def connectionSecure(self):
        self.requestService(ClientUserAuth(self._user, self.conn))


class ClientUserAuth(userauth.SSHUserAuthClient):
    def getPassword(self, prompt = None):
        return

    def getPublicKey(self):
        return PUBKEY

    def getPrivateKey(self):
        return defer.succeed(PRIVKEY)


class ClientConnection(connection.SSHConnection):
    sftp = None
    def __init__(self):
        connection.SSHConnection.__init__(self)

        self._parts = {}
        self._waiting = defaultdict(list)

    def serviceStarted(self):
        self.openChannel(LSChannel())

    def __getitem__(self, name):
        if name in self._parts:
            return defer.succeed(self._parts[name])
        else:
            dfd = defer.Deferred()
            self._waiting[name].append(dfd)
            return dfd

    def __setitem__(self, name, val):
        self._parts[name] = val
        waiting_dfds = self._waiting.pop(name)
        for dfd in waiting_dfds:
            dfd.callback(val)


from twisted.conch.ssh.filetransfer import FileTransferClient, FXF_READ, FXF_WRITE


class LSChannel(channel.SSHChannel):
    name = 'session'

    def channelOpen(self, data):
        d = self.conn.sendRequest(self, 'subsystem', common.NS('sftp'), wantReply=True)
        d.addCallback(self._cbSFTP)

    def _cbSFTP(self, result):
        client = FileTransferClient()
        client.makeConnection(self)
        self.dataReceived = client.dataReceived
        self.client = client
        self.conn['sftp'] = self

    @defer.inlineCallbacks
    def __getitem__(self, directory):
        dir_files = yield self.client.openDirectory(directory)
        all_files = []

        for f in dir_files:
            try:
                ff = yield f
                all_files.append(ff)
            except EOFError:
                break
        dir_files.close()
        defer.returnValue(all_files)

    def __getattr__(self, name):
        return getattr(self.client, name)

    def __hash__(self):
        return object.__hash__(self)

class CommandChannel(channel.SSHChannel):

    name = 'session'
    def __init__(self, cmd, *args, **kwargs):
        self._cmd = cmd
        channel.SSHChannel.__init__(*args, **kwargs)

    def channelOpen(self, data):
        d = self.conn.sendRequest(self, 'exec', common.NS(self._cmd),
                                  wantReply = 1)
        d.addCallback(self._cbSendRequest)
        self._received = []

    def _cbSendRequest(self, ignored):
        self.conn.sendEOF(self)
        self.loseConnection()

    def dataReceived(self, data):
        self._received.append(data)

    def closed(self):
        resp = ''.join(self._received)


class InputProto(LineReceiver):
    delimiter = b'\x00'
    def __init__(self, ctrl):
        self._ctrl = ctrl

    def lineReceived(self, line):
        command = json.loads(line)

        handler = getattr(self, 'handle_' + command['cmd'].lower(), None)
        if handler is None:
            return

        (handler(command['user'], command['host'], command['port'], command.get('args', None))
            .addCallback(self._gotResponse, command['id'])
            .addErrback(log.err)
        )

    @defer.inlineCallbacks
    def handle_directory(self, user, host, port, directory):
        directory = directory.encode('utf-8')
        key = (user, host, port)
        conn = yield self._ctrl[key]
        sftp = yield conn['sftp']
        files = yield sftp[directory]
        realPath = yield sftp.realPath(directory)
        defer.returnValue({'files': files, 'path': realPath})

    def _gotResponse(self, response, id):
        data = json.dumps({
            'response': response,
            'id': id
        })

        self.sendLine(data.encode('utf-8').strip())
    @defer.inlineCallbacks
    def handle_file(self, user, host, port, filename):
        filename = filename.encode('utf-8')
        key = (user, host, port)
        conn = yield self._ctrl[key]
        sftp = yield conn['sftp']
        sftpfile = yield sftp.openFile(filename, FXF_READ, {})

        filedata = []
        bytesRead = 0
        while True:
            try:
                chunk = yield sftpfile.readChunk(bytesRead, 1024 * 16)
            except EOFError:
                break
            bytesRead += len(chunk)
            filedata.append(chunk)

        defer.returnValue({'file': ''.join(filedata)})


    def _gotResponse(self, response, id):
        data = json.dumps({
            'response': response,
            'id': id
        })

        self.sendLine(data.encode('utf-8').strip())
from twisted.internet.endpoints import TCP4ClientEndpoint, connectProtocol

class RemoteConnection(object):
    def __init__(self):
        self._connecting = {}
        self._waiting = defaultdict(list)
        self._protocols = {}

    def __getitem__(self, (user, host, port)):
        key = (user, host, port)

        if key in self._protocols:
            return defer.succeed(self._protocols[key].conn)

        if key not in self._connecting:
            user = user.encode('utf-8')
            host = host.encode('utf-8')
            ep = TCP4ClientEndpoint(reactor, host, port)
            conn = ClientConnection()
            proto = ClientTransport(user, conn)
            self._connecting[key] = connectProtocol(ep, proto).addCallback(self._onConnected, key)

        dfd = defer.Deferred()
        self._waiting[key].append(dfd)
        return dfd.addCallback(lambda sshtransport: sshtransport.conn)

    def _onConnected(self, proto, key):
        waiting = self._waiting.pop(key)
        for dfd in waiting:
            dfd.callback(proto)
        self._protocols[key] = proto
        self._connecting.pop(key)

    def runCommand(self, user, host, port, cmd):
        cmd = cmd.encode('utf-8')
        key = (user, host, port)
        return self[key].addCallback(self._doRunCommand, cmd)

    def _doRunCommand(self, proto, cmd):
        conn = proto.conn


c = RemoteConnection()

log.startLogging(sys.stderr)
stdio.StandardIO(InputProto(c))
reactor.run()
