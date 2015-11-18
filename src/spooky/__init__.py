# Copyright (C) 2015 Stanford University
# Contact: Niels Joubert <niels@cs.stanford.edu>
#
# A Zero-Dependency Network Management Library
#

__all__ = ["ip", "modules"]

#====================================================================#

import time, socket, sys, os, sys, inspect, signal, traceback
import argparse, json

import threading
import collections
from contextlib import closing

import subprocess

def get_version():
  return subprocess.check_output(["git", "describe", "--dirty", "--always"]).strip()

#====================================================================#

class Configuration(object):
  '''
  Attempts to be threadsafe.

  Assumes your config is a JSON Object containing at leasT:
    'GLOBALS'
    'NETWORK'
    '<my identifier>'
    '<foreign identifiers>'

  And each identifier contains a list of key-value pairs.
  Thread-safety is only valid for this two-level structure.
  If the second level contains complex objects, thread-safety of 
  modifications the returned value is not guaranteed.
  '''

  def __init__(self, filename, ident):
    self._lock = threading.Lock()
    self.load(filename, ident)

  def load(self, filename, ident):
    self._lock.acquire()
    with open(filename) as data_file:    
      CONFIG = json.load(data_file)
    self.data = CONFIG
    self.ident = ident
    self._lock.release()

  def set_my(self, key, value):
    try:
      self._lock.acquire()
      if key in self.data['GLOBALS']:
        self.data['GLOBALS'][key] = value
      else: 
       self.data[self.ident][key] = value
    finally:
      self._lock.release()

  def get_my(self, key):
    try:
      self._lock.acquire()
      value = None
      if key in self.data[self.ident]:
        value = self.data[self.ident][key]
      elif key in self.data['GLOBALS']:
        value = self.data['GLOBALS'][key]
      if value == None:
        raise KeyError(key)
      else:
        return value
    finally:
      self._lock.release()

  def get_foreign(self, ident, key):
    try:
      self._lock.acquire()
      value = self.data[ident][key]
      return value
    finally:
      self._lock.release()
  
  def get_network(self, key):
    try:
      self._lock.acquire()
      value = self.data['NETWORK'][key]
      return value
    finally:
      self._lock.release()

  def __str__(self):
    z = self.data[self.ident].copy()
    z.update(self.data['GLOBALS'])
    return z.__str__()

  def cmd_config(self, args):
    if len(args) < 1:
      cmd = "list"
    else:
      cmd = args[0].strip()

    if cmd == "help":
      print "config <list|set|unset>"
    elif cmd == "list":
      import pprint
      self._lock.acquire()
      pp = pprint.PrettyPrinter(indent=4)
      pp.pprint(self.data)
      self._lock.release()

#====================================================================#

class BufferedUDPSocket(object):

  def __init__(self):
    self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    self._databuffer = collections.deque()
    self.recv_size = 8192

  def __getattr__(self, name):
    return getattr(self._sock, name)

  def recv(self, bufsize, *flags):
    data, addr = self.recvfrom(bufsize)
    return data

  def recvfrom(self, bufsize, *flags):
    recvd, addr = self._sock.recvfrom(self.recv_size)
    try:
      self._databuffer.extend(recvd)
      data = collections.deque()
      while len(data) < bufsize:
        data.append(self._databuffer.popleft())
      return "".join(data), addr
    except IndexError:
      return "".join(data), addr

#====================================================================#

class UDPBroadcaster(object):
  ''' 
  Defines a udp socket in broadcast mode, 
  along with a helper broadcast function
  '''

  def __init__(self, dest=('192.168.2.255', 5000)):
    self.dest = dest
    self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

  def broadcast(self, msg):
    self.udp.sendto(msg, self.dest)

#====================================================================#

class UDPBroadcastListener(object):
  '''
  Defines a udp socket bound to a broadcast address,
  along with a helper recvfrom function.
  DOES TIMEOUT!
  '''

  def __init__(self, port=5000, timeout=1.0):
    self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    self.udp.settimeout(timeout) # Might be unnecessary with daemon=True
    self.udp.bind(('', port))

  def recvfrom(self, buffsize):
    return self.udp.recvfrom(buffsize)

#====================================================================#    

class UDPBroadcastListenerHandlerThread(threading.Thread, UDPBroadcastListener):
  '''
  Very simple repeater:
    Listens for broadcast data coming in on given port
    Send this data to the given data_callback.
  '''

  def __init__(self, main, data_callback, port=5000):
    threading.Thread.__init__(self)
    UDPBroadcastListener.__init__(self, port=port)
    self.data_callback = data_callback
    self.daemon = True
    self.dying = False

  def run(self):
    while not self.dying:
      try:
          msg, addr = self.recvfrom(4096)
          if msg:
            self.data_callback(msg)
      except (KeyboardInterrupt, SystemExit):
        raise
      except socket.timeout:
        if self.dying:
          return
      except socket.error:
        traceback.print_exc()

  def stop(self):
    self.dying = True
    self.udp.setblocking(0)

#====================================================================#

class SimpleScheduler:
  """
  SimpleScheduler

  Provides a simple top-level scheduler for your application.
  Each task is represented by a function call.
  SimpleScheduler calls a task at a regular, given interval.

  Call tick() from a mainloop at a regular interval
  Call addTask() to to register tasks in the form of function calls 
  """
  def __init__(self):
    self.tasks = []
    self.lastMs = time.time() * 1e3
    self.avgSleepMs = 0
    self.addTask(1000, self.monitor)

  def addTask(self, run_every_ms, fnct):
    self.tasks.append({'f':fnct, 'run_every': run_every_ms, 'last_run': 0})

  def tick(self):
    currentMs = time.time() * 1e3
    
    for task in self.tasks:
      if currentMs - task['last_run'] > task['run_every']:
        task['last_run'] = currentMs
        task['f']()

    currentMs = time.time() * 1e3
    minUntilNext = 1000
    for task in self.tasks:
      stillWaiting = max(task['run_every'] - (currentMs - task['last_run']), 0)
      minUntilNext = min(minUntilNext, stillWaiting)

    self.avgSleepMs = self.avgSleepMs * 0.7 + minUntilNext * 0.3
    time.sleep(minUntilNext / 1e3)

  def monitor(self):
    print "Scheduler sleeping around", self.avgSleepMs, "ms between tasks"

#====================================================================#

class Multicast:
  """
  Multicast provides a homebrewed and inefficient but simple multicast solution
  Multicast simply repeats a packet to all registered sockets. 
  """
  def __init__(self):
    self.recipients = set([])

  def sendto(self, udp, data):
    for addr in self.recipients:
      udp.sendto(data, addr)

  def addRecipient(self, addr):
    self.recipients = self.recipients.union([addr])

  def __str__(self):
    print self.recipients

#====================================================================#
