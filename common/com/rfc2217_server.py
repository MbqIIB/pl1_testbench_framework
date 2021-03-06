#!/usr/bin/env python

# (C) 2009 Chris Liechti <cliechti@gmx.net>
# (C) 2013-2014 Bernard Blackham
# redirect data from a TCP/IP connection to a serial port and vice versa
# using RFC 2217


import sys
import threading
import time
import select
import socket
import serial
import serial.rfc2217
import logging

class Redirector:
    def __init__(self, serial_instance, socket, debug=None):
        self.serial = serial_instance
        self.socket = socket
        self._write_lock = threading.Lock()
        self.rfc2217 = serial.rfc2217.PortManager(
            self.serial,
            self,
            logger = (debug and logging.getLogger('rfc2217.server'))
            )
        self.log = logging.getLogger('redirector')

    def statusline_poller(self):
        self.log.debug('status line poll thread started')
        while self.alive:
            time.sleep(1)
            self.log.debug('checking modem lines')
            self.rfc2217.check_modem_lines()
            self.log.debug('status line poll')
        self.log.debug('status line poll thread terminated')

    def shortcut(self):
        """connect the serial port to the TCP port by copying everything
           from one side to the other"""
        self.alive = True
        self.thread_read = threading.Thread(target=self.reader)
        self.thread_read.setDaemon(True)
        self.thread_read.setName('serial->socket')
        self.thread_read.start()
        self.thread_poll = threading.Thread(target=self.statusline_poller)
        self.thread_poll.setDaemon(True)
        self.thread_poll.setName('status line poll')
        self.thread_poll.start()
        self.writer()

    def reader(self):
        """loop forever and copy serial->socket"""
        self.log.debug('reader thread started')
        while self.alive:
            try:
                data = self.serial.read(1)              # read one, blocking
                self.log.debug('read: %s' % (data.strip()))
                n = self.serial.inWaiting()             # look if there is more
                if n:
                    self.log.debug('reading more')
                    data = data + self.serial.read(n)   # and get as much as possible
                    self.log.debug('read more: %s' % (data.strip()))
                if data:
                    # escape outgoing data when needed (Telnet IAC (0xff) character)
                    self.log.debug('sending data to TCP')
                    data = serial.to_bytes(self.rfc2217.escape(data))
                    self._write_lock.acquire()
                    try:
                        self.socket.sendall(data)       # send it over TCP
                    finally:
                        self._write_lock.release()
                    self.log.debug('sent it')
            except Exception, msg:
                self.log.error('%s' % (msg,))
                # probably got disconnected
                break
        self.alive = False
        self.socket.shutdown(socket.SHUT_RDWR)
        self.socket.close()
        self.log.debug('reader thread terminated')

    def write(self, data):
        """thread safe socket write with no data escaping. used to send telnet stuff"""
        self._write_lock.acquire()
        try:
            self.socket.sendall(data)
        finally:
            self._write_lock.release()

    def writer(self):
        """loop forever and copy socket->serial"""
        self.socket.setblocking(0)
        while self.alive:
            try:
                ready = select.select([self.socket], [], [], 1.0)
                if not ready[0]:
                    continue
                data = self.socket.recv(1024)
                if data == '':
                    self.log.error('recv EOF')
                    break
                self.log.debug('writing: %s' % (data.strip()))
                self.serial.write(serial.to_bytes(self.rfc2217.filter(data)))
            except Exception, msg:
                self.alive = False
                self.log.error('%s' % (msg,))
                # probably got disconnected
                break
        self.stop()

    def stop(self):
        """Stop copying"""
        self.log.debug('stopping')
        if self.alive:
            self.socket.shutdown(socket.SHUT_RDWR)
            self.alive = False
            self.thread_read.join()
            self.thread_poll.join()


if __name__ == '__main__':
    import optparse

    parser = optparse.OptionParser(
        usage = "%prog [options] port",
        description = "RFC 2217 Serial to Network (TCP/IP) redirector.",
        epilog = """\
NOTE: no security measures are implemented. Anyone can remotely connect
to this service over the network.

Only one connection at once is supported. When the connection is terminated
it waits for the next connect.

Example:
  C:\Python27\Python.exe rfc2217_server.py COM158
  /usb/bin/python rfc2217_server.py /dev/ttyACM0
""")

    parser.add_option("-p", "--localport",
        dest = "local_port",
        action = "store",
        type = 'int',
        help = "local TCP port",
        default = 2217
    )

    parser.add_option("-v", "--verbose",
        dest = "verbosity",
        action = "count",
        help = "print more diagnostic messages (option can be given multiple times)",
        default = 0
    )

    (options, args) = parser.parse_args()

    if len(args) != 1:
        parser.error('serial port name required as argument')

    if options.verbosity > 3:
        options.verbosity = 3
    level = (
        logging.WARNING,
        logging.INFO,
        logging.DEBUG,
        logging.NOTSET,
        )[options.verbosity]
    logging.basicConfig(level=logging.INFO)
    logging.getLogger('root').setLevel(logging.INFO)
    logging.getLogger('rfc2217').setLevel(level)
    logging.getLogger('redirector').setLevel(level)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind( ('', options.local_port) )
    srv.listen(1)
    logging.info("TCP/IP port: %s" % (options.local_port,))

    while True:
        logging.info("RFC 2217 TCP/IP to Serial redirector - type Ctrl-C / BREAK to quit")

        try:
            connection, addr = srv.accept()
            logging.info('Connected by %s:%s' % (addr[0], addr[1]))
            connection.setsockopt( socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            # connect to serial port
            ser = serial.Serial()
            ser.port     = args[0]
            ser.timeout  = 3     # required so that the reader thread can exit

            try:
                ser.open()
            except serial.SerialException, e:
                logging.error("Could not open serial port %s: %s" % (ser.portstr, e))
                time.sleep(2)
                continue
                #sys.exit(1)

            logging.info("Serving serial port: %s" % (ser.portstr,))
            settings = ser.getSettingsDict()
            ser.setRTS(True)
            ser.setDTR(True)
            # enter network <-> serial loop
            r = Redirector(
                ser,
                connection,
                options.verbosity > 0
            )
            try:
                r.shortcut()
            finally:
                logging.info('Disconnected')
                r.stop()
                connection.close()
                ser.setDTR(False)
                ser.setRTS(False)
            # Restore port settings (may have been changed by RFC 2217 capable
            # client)
            ser.applySettingsDict(settings)
            ser.close()
        except KeyboardInterrupt:
            break
        except socket.error, msg:
            logging.error('%s' % (msg,))

    logging.info('--- exit ---')
