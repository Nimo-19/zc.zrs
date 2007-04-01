##############################################################################
#
# Copyright (c) 2006 Zope Corporation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.0 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################
import cPickle, logging, os, shutil, struct, sys
import tempfile, threading, time, unittest

import transaction
from ZODB.TimeStamp import TimeStamp
import ZODB.utils

from zope.testing import doctest, setupstack

import twisted.internet.error
import twisted.python.failure

import zc.zrs.fsiterator
import zc.zrs.sizedmessage


def scan_from_back():
    r"""
Create the database:

    >>> import ZODB.FileStorage
    >>> from ZODB.DB import DB
    >>> import persistent.dict

    >>> fs = ZODB.FileStorage.FileStorage('Data.fs')
    >>> db = DB(fs)
    >>> conn = db.open()

    >>> for i in range(100):
    ...     conn.root()[i] = persistent.dict.PersistentDict()
    ...     commit()

Now, be evil, and muck up the beginning: :)

    >>> fs._file.seek(12)
    >>> fs._file.write('\xff'*8)
    >>> conn.root()[100] = persistent.dict.PersistentDict()
    >>> commit()

If we try to iterate from the beginning, we'll get an error:

    >>> condition = threading.Condition()
    >>> it = zc.zrs.fsiterator.FileStorageIterator(fs, condition)
    >>> it.next()
    Traceback (most recent call last):
    ...
    CorruptedDataError: Error reading unknown oid.  Found '' at 4

    >>> def tid_from_time(t):
    ...     return repr(TimeStamp(*(time.gmtime(t)[:5] + (t%60,))))

    >>> tid = tid_from_time(time.time()-70)
    >>> zc.zrs.fsiterator.FileStorageIterator(fs, condition, tid)
    Traceback (most recent call last):
    ...
    OverflowError: long too big to convert

But, if we iterate from near the end, we'll be OK:

    >>> tid = tid_from_time(time.time()-30)
    >>> it = zc.zrs.fsiterator.FileStorageIterator(fs, condition, tid)
    >>> trans = it.next()
    >>> from ZODB import utils
    >>> print TimeStamp(trans.tid), [utils.u64(r.oid) for r in trans]
    2007-03-21 20:34:09.000000 [0L, 72L]

    >>> print TimeStamp(tid)
    2007-03-21 20:34:08.000000

    >>> tid = tid_from_time(time.time()-29.5)
    >>> it = zc.zrs.fsiterator.FileStorageIterator(fs, condition, tid)
    >>> trans = it.next()
    >>> from ZODB import utils
    >>> print TimeStamp(trans.tid), [utils.u64(r.oid) for r in trans]
    2007-03-21 20:34:09.000000 [0L, 72L]

    >>> print TimeStamp(tid)
    2007-03-21 20:34:08.500000

    """

def primary_suspend_resume():
    """
The primary producer is supposed to be suspendable.

We'll create a file-storage:

    >>> import ZODB.FileStorage
    >>> fs = ZODB.FileStorage.FileStorage('Data.fs')
    >>> from ZODB.DB import DB
    >>> db = DB(fs)

Now, we'll create an iterator:

    >>> import zc.zrs.fsiterator
    >>> iterator = zc.zrs.fsiterator.FileStorageIterator(fs)

And a special transport that will output data when it is called:

    >>> class Reactor:
    ...     def callFromThread(self, f, *args, **kw):
    ...         f(*args, **kw)

    >>> class Transport:
    ...     def __init__(self):
    ...         self.reactor = Reactor()
    ...     def write(self, message):
    ...         message = message[4:] # cheat. :)
    ...         print cPickle.loads(message)[0]
    ...     def registerProducer(self, producer, streaming):
    ...         print 'registered producer'
    ...     def unregisterProducer(self):
    ...         print 'unregistered producer'
    ...     def loseConnection(self):
    ...         print 'loseConnection'

And a producer based on the iterator and transport:

    >>> import zc.zrs.primary
    >>> import time
    >>> producer = zc.zrs.primary.PrimaryProducer(iterator, Transport(), 'test'
    ...            ); time.sleep(0.1)
    registered producer
    T
    S
    C

We get the initial transaction, because the producer starts producing
immediately.  Let's oause producing:

    >>> producer.pauseProducing()

and we'll create another transaction:

    >>> conn = db.open()
    >>> ob = conn.root()
    >>> import persistent.dict
    >>> ob.x = persistent.dict.PersistentDict()
    >>> commit()
    >>> iterator.notify()
    >>> ob = ob.x
    >>> ob.x = persistent.dict.PersistentDict()
    >>> commit()
    >>> iterator.notify()
    >>> time.sleep(0.1)
    
No output because we are paused.  Now let's resume:

    >>> producer.resumeProducing(); time.sleep(0.1)
    T
    S
    S
    C
    T
    S
    S
    C

and pause again:

    >>> producer.pauseProducing()
    >>> ob = ob.x
    >>> ob.x = persistent.dict.PersistentDict()
    >>> commit()
    >>> iterator.notify()
    >>> time.sleep(0.1)

and resume:

    >>> producer.resumeProducing(); time.sleep(0.1)
    T
    S
    S
    C

    >>> producer.close()
    unregistered producer
    loseConnection

    >>> db.close()

"""

class TestReactor:

    def __init__(self):
        self._factories = {}
        self.clients = {}
        self.client_port = 47245
            
    def listenTCP(self, port, factory, backlog=50, interface=''):
        addr = interface, port
        assert addr not in self._factories
        self._factories[addr] = factory

    def connect(self, addr):
        proto = self._factories[addr].buildProtocol(addr)
        transport = PrimaryTransport(
            proto, "IPv4Address(TCP, '127.0.0.1', %s)" % self.client_port)
        self.client_port += 1
        transport.reactor = self
        proto.makeConnection(transport)
        return transport

    def callFromThread(self, f, *a, **k):
        f(*a, **k)

    def connectTCP(self, host, port, factory, timeout=30):
        addr = host, port
        proto = factory.buildProtocol(addr)
        transport = SecondaryTransport(
            proto, "IPv4Address(TCP, '127.0.0.1', %s)" % self.client_port)
        self.client_port += 1
        transport.reactor = self
        transport.factory = factory
        proto.makeConnection(transport)
        connector = None # I wonder what this should be :)
        factory.startedConnecting(connector)
        self.clients.setdefault(addr, []).append(transport)

close_reason = twisted.python.failure.Failure(
    twisted.internet.error.ConnectionDone())

class MessageTransport:

    def __init__(self, proto, peer):
        self.data = ''
        self.cond = threading.Condition()
        self.closed = False
        self.proto = proto
        self.peer = peer

    def getPeer(self):
        return self.peer
        
    def write(self, data):
        self.cond.acquire()
        self.data += data
        self.cond.notifyAll()
        self.cond.release()

    def writeSequence(self, data):
        self.write(''.join(data))

    def read(self):
        self.cond.acquire()

        if len(self.data) < 4:
            self.cond.wait(5)
            assert len(self.data) >= 4
        l, = struct.unpack(">I", self.data[:4])
        self.data = self.data[4:]
        
        if len(self.data) < l:
            self.cond.wait(5)
            assert len(self.data) >= l, (l, len(self.data))
        result = self.data[:l]
        self.data = self.data[l:]

        self.cond.release()

        return result

    def send(self, data):
        self.proto.dataReceived(zc.zrs.sizedmessage.marshal(data))

    def have_data(self):
        return bool(self.data)

    def loseConnection(self):
        self.closed = True
        if self.producer is None:
            self.proto.connectionLost(close_reason)

    producer = None
    def registerProducer(self, producer, streaming):
        self.producer = producer

    def unregisterProducer(self):
        if self.producer is not None:
            self.producer = None
            if self.closed:
                self.proto.connectionLost(close_reason)

    def close(self):
        self.producer.stopProducing()
        self.proto.connectionLost(close_reason)

class PrimaryTransport(MessageTransport):

    def read(self):
        return cPickle.loads(MessageTransport.read(self))

class SecondaryTransport(MessageTransport):
    
    def send(self, data):
        MessageTransport.send(self, cPickle.dumps(data))

class Stdout:
    def write(self, data):
        sys.stdout.write(data)
    def flush(self):
        sys.stdout.flush()

stdout_handler = logging.StreamHandler(Stdout())

def join(old):
    # Wait for any new threads created during a test to die.
    for thread in threading.enumerate():
        if thread not in old:
            thread.join(1.0)

def setUp(test):
    setupstack.register(test, join, threading.enumerate())
    setupstack.setUpDirectory(test)
    global now
    now = time.mktime((2007, 3, 21, 15, 32, 57, 2, 80, 0))
    oldtime = time.time
    setupstack.register(test, lambda : setattr(time, 'time', oldtime))
    time.time = lambda : now
    def commit():
        global now
        now += 1
        transaction.commit()
    test.globs['commit'] = commit

    test.globs['reactor'] = TestReactor()

    logger = logging.getLogger('zc.zrs')
    logger.setLevel(1)
    setupstack.register(test, logger.setLevel, 0)
    logger.addHandler(stdout_handler)
    setupstack.register(test, logger.removeHandler, stdout_handler)

def test_suite():
    return unittest.TestSuite((
        doctest.DocFileSuite(
            'fsiterator.txt', 'primary.txt', 'secondary.txt',
            setUp=setUp, tearDown=setupstack.tearDown),
        doctest.DocTestSuite(
            setUp=setUp, tearDown=setupstack.tearDown),
        ))

if __name__ == '__main__':
    unittest.main(defaultTest='test_suite')

