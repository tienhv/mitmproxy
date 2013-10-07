#!/usr/bin/env python2.7
'''
Common MITM proxy classes.
'''

from twisted.internet import protocol, reactor, defer
import Queue
import optparse
import time
import sys
import string
import re


class MITMException(Exception):
    pass


class ProxyOptionParser():
    def __init__(self, port, localPort):
        self.parser = optparse.OptionParser()
        self.parser.add_option(
            '-H', '--host', dest='host', type='string',
            metavar='HOST', default='localhost',
            help='Hostname/IP of real server')
        self.parser.add_option(
            '-P', '--port', dest='port', type='int',
            metavar='PORT', default=port,
            help='Port of real server')
        self.parser.add_option(
            '-p', '--local-port', dest='localPort', type='int',
            metavar='PORT', default=localPort,
            help='Local port to listen on')
        self.parser.add_option(
            '-o', '--output', dest='logFile', type='string',
            metavar='FILE', default=None,
            help='Save log to FILE instead of writing to stdout')
        self.opts, self.args = self.parser.parse_args()


class ReplayOptionParser():
    def __init__(self, localPort):
        self.parser = optparse.OptionParser()
        self.parser.add_option(
            '-p', '--local-port', dest='localPort', type='int',
            metavar='PORT', default=localPort,
            help='Local port to listen on')
        self.parser.add_option(
            '-f', '--from-file', dest='inputFile', type='string',
            metavar='FILE', default=None,
            help='Read session capture from FILE instead of STDIN')
        self.parser.add_option(
            '-o', '--output', dest='logFile', type='string',
            metavar='FILE', default=None,
            help='Log into FILE instead of STDOUT')
        self.parser.add_option(
            '-d', '--delay-modifier', dest='delayMod', type='float',
            metavar='FLOAT', default=1.0,
            help='Modify response delay (default: 1.0 - no change)')
        self.opts, self.args = self.parser.parse_args()


class ViewerOptionParser():
    def __init__(self):
        self.parser = optparse.OptionParser()
        self.parser.add_option(
            '-f', '--from-file', dest='inputFile', type='string',
            metavar='FILE', default=None,
            help='Read session capture from FILE instead of STDIN')
        self.parser.add_option(
            '-d', '--delay-modifier', dest='delayMod', type='float',
            metavar='FLOAT', default=1.0,
            help='Modify response delay (default: 1.0 - no change)')
        self.opts, self.args = self.parser.parse_args()


filter = ''.join(
    [['.', chr(x)][chr(x) in string.printable[:-5]] for x in xrange(256)])


class Logger():
    '''
    logs telnet traffic to STDOUT/file (TAB-delimited)
    format: "time_since_start client/server 0xHex_data"
    eg. "0.0572540760 server 0x0a0d55736572204e616d65203a20 #plaintext"
        "0.1084461212 client 0x6170630a #plaintext"
    '''

    def __init__(self):
        self._startTime = None
        self._logFile = None

    def openLog(self, filename):
        '''
        Set up a file for writing a log into it.
        If not called, log is written to STDOUT.
        '''
        self._logFile = open(filename, 'w')

    def closeLog(self):
        '''
        Try to close a possibly open log file.
        '''
        if self._logFile is not None:
            self._logFile.close()
            self._logFile = None

    def log(self, who, what):
        '''
        Add a new message to log.
        '''
        # translate non-printable chars to dots
        plain = what.decode('hex').translate(filter)

        if self._startTime is None:
            self._startTime = time.time()

        timestamp = time.time() - self._startTime

        if self._logFile is not None:
            # write to a file
            self._logFile.write(
                "%0.10f\t%s\t0x%s\t#%s\n"
                % (timestamp, who, what, plain))
        else:
            # STDOUT output
            sys.stdout.write(
                "%0.10f\t%s\t0x%s\t#%s\n"
                % (timestamp, who, what, plain))


class ProxyProtocol(protocol.Protocol):
    '''
    Protocol class common to both client and server.
    '''
    def proxyDataReceived(self, data):
        '''
        Callback function for both client and server side of the proxy.
        Each side specifies its input (rx) and output (tx) queues.
        '''
        if data is False:
            # Special value indicating that one side of our proxy
            # no longer has an open connection. So we close the
            # other end.
            self.rx = None
            self.transport.loseConnection()
            # the reactor should be stopping just about now
        elif self.tx is not None:
            # Transmit queue is defined => connection to
            # the other side is still open, we can send data to it.
            self.transport.write(data)
            self.rx.get().addCallback(self.proxyDataReceived)
        else:
            # got some data to be sent, but we no longer
            # have a connection to the other side
            sys.stderr.write(
                'Unable to send queued data: not connected to %s.\n'
                % (self.origin))
            # the other proxy instance should already be calling
            # reactor.stop(), so we can just take a nap

    def dataReceived(self, data):
        '''
        Received something from out input. Put it into the output queue.
        '''
        self.log.log(self.origin, data.encode('hex'))
        self.tx.put(data)

    def connectionLost(self, reason):
        '''
        Either end of the proxy received a disconnect.
        '''
        if self.origin == 'server':
            sys.stderr.write('Disconnected from real server.\n')
        else:
            sys.stderr.write('Client disconnected.\n')
        self.log.closeLog()
        # destroy the receive queue
        self.rx = None
        # put a special value into tx queue to indicate connecion loss
        self.tx.put(False)
        # stop the program
        if reactor.running:
            reactor.stop()


class ProxyClient(ProxyProtocol):
    def connectionMade(self):
        '''
        Successfully established a connection to the real server.
        '''
        sys.stderr.write('Connected to real server.\n')
        self.origin = self.factory.origin
        # input - data from the real server
        self.rx = self.factory.sq
        # output - data for the real client
        self.tx = self.factory.cq
        self.log = self.factory.log

        # callback for the receiver queue
        self.rx.get().addCallback(self.proxyDataReceived)


class ProxyClientFactory(protocol.ClientFactory):
    protocol = ProxyClient

    def __init__(self, sq, cq, log):
        # which side we're talking to?
        self.origin = 'server'
        self.sq = sq
        self.cq = cq
        self.log = log

    def clientConnectionFailed(self, connector, reason):
        self.cq.put(False)
        sys.stderr.write('Unable to connect! %s\n' % reason.getErrorMessage())


class ProxyServer(ProxyProtocol):
    '''
    Server part of the MITM proxy.
    '''
    def __init__(self):
        # proxy server initialized, connect to real server
        sys.stderr.write(
            'Connecting to %s:%d...\n' % (self.host, self.port))
        self.connectToServer()

    def connectToServer(self):
        '''
        Example:
            factory = mitmproxy.ProxyClientFactory(
                self.factory.sq, self.factory.cq, self.log)
            reactor.connect[PROTOCOL](
                self.host, self.port, factory [, OTHER_OPTIONS])
        '''
        raise MITMException('You should implement this method in your code.')

    def connectionMade(self):
        '''
        Unsuspecting client connected to our fake server. *evil grin*
        '''
        # add callback for the receiver queue
        self.rx.get().addCallback(self.proxyDataReceived)
        sys.stderr.write('Client connected.\n')


class ProxyServerFactory(protocol.ServerFactory):
    def __init__(self, protocol, host, port, log):
        self.protocol = protocol
        # which side we're talking to?
        self.protocol.origin = "client"
        self.protocol.host = host
        self.protocol.port = port
        self.protocol.log = log
        self.protocol.rx = defer.DeferredQueue()
        self.protocol.tx = defer.DeferredQueue()


class ReplayServer(protocol.Protocol):
    def connectionMade(self):
        sys.stderr.write('Client connected.\n')
        self.sendNext()

    def sendNext(self):
        '''
        Called after the client connects.
        We shall send (with a delay) all the messages
        from our queue until encountering either None or
        an exception. In case a reply is not expected from
        us at this time, the head of queue will hold None
        (client is expected to send more messages before
        we're supposed to send a reply) - so we just "eat"
        the None from head of our queue (sq).
        '''
        while True:
            try:
                # gets either:
                #  * a message - continue while loop (send the message)
                #  * None - break from the loop (client talks next)
                #  * Empty exception - close the session
                reply = self.sq.get(False)
                if reply is None:
                    break
            except Queue.Empty:
                # both cq and sq empty -> close the session
                sys.stderr.write('Success.\n')
                self.success = True
                self.log.closeLog()
                self.transport.loseConnection()
                break

            (delay, what) = reply
            self.log.log('server', what)
            # sleep for a while (read from proxy log),
            # modified by delayMod
            time.sleep(delay * self.delayMod)
            self.transport.write(what.decode('hex'))

    def dataReceived(self, data):
        '''
        Called when client send us some data.
        Compare received data with expected message from
        the client message queue (cq), report mismatch (if any)
        try sending a reply (if available) by calling sendNext().
        '''
        try:
            expected = self.cq.get(False)
        except Queue.Empty:
            raise MITMException("Nothing more expected in this session.")

        exp_hex = expected[1]
        got_hex = data.encode('hex')

        if got_hex == exp_hex:
            self.log.log('client', expected[1])
            self.sendNext()
        else:
            # received something else, terminate
            sys.stderr.write(
                "ERROR: Expected %s (%s), got %s (%s).\n"
                % (exp_hex, exp_hex.decode('hex').translate(filter),
                    got_hex, got_hex.decode('hex').translate(filter)))
            self.log.closeLog()
            if reactor.running:
                reactor.stop()

    def connectionLost(self, reason):
        '''
        Remote end closed the session.
        '''
        if not self.success:
            sys.stderr.write('FAIL! Premature end: not all messages sent.\n')
        sys.stderr.write('Client disconnected.\n')
        self.log.closeLog()
        if reactor.running:
            reactor.stop()


class ReplayServerFactory(protocol.ServerFactory):
    protocol = ReplayServer

    def __init__(self, log, sq, cq, delayMod, clientFirst):
        self.protocol.log = log
        self.protocol.sq = sq
        self.protocol.cq = cq
        self.protocol.delayMod = delayMod
        self.protocol.clientFirst = clientFirst
        self.protocol.success = False


class LogReader():
    '''
    Read the whole proxy log into two separate queues,
    one with the expected client messages (cq) and the
    other containing the replies that should be sent
    to the client.
    '''
    def __init__(self, inputFile, sq, cq, clientFirst):
        with open(inputFile) as inFile:
            lastTime = 0
            for line in inFile:
                # optional fourth field contains comments,
                # usually an ASCII representation of the data
                (timestamp, who, what, _) = line.rstrip('\n').split('\t')

                # if this is the first line of log, determine who said it
                if clientFirst is None:
                    if who == "client":
                        clientFirst = True
                    else:
                        clientFirst = False

                # strip the pretty-print "0x" prefix from hex data
                what = what[2:]
                # compute the time between current and previous msg
                delay = float(timestamp) - lastTime
                lastTime = float(timestamp)

                if who == 'server':
                    # server reply queue
                    sq.put([delay, what])
                elif who == 'client':
                    # put a sync mark into server reply queue
                    # to distinguish between cases of:
                    #  * reply consists of a single packet
                    #  * more packets
                    sq.put(None)  # sync mark
                    # expected client messages
                    cq.put([delay, what])
                else:
                    raise MITMException('Malformed proxy log!')


class LogViewer():
    '''
    Loads and simulates a given log file in either real-time
    or dilated by a factor of delayMod.
    '''
    def __init__(self, inputFile, delayMod):
        with open(inputFile) as inFile:
            lastTime = 0
            for line in inFile:
                # optional fourth field contains comments,
                # usually an ASCII representation of the data
                (timestamp, who, what, _) = line.rstrip('\n').split('\t')
                # strip the pretty-print "0x" prefix from hex data
                what = what[2:]
                # strip telnet IAC sequences
                what = re.sub('[fF][fF]....', '', what)
                # compute the time between current and previous msg
                delay = float(timestamp) - lastTime
                lastTime = float(timestamp)

                # wait for it...
                time.sleep(delay * delayMod)

                if who == 'server':
                    sys.stdout.write(what.decode('hex'))
