# Copyright (C) 2015 Stanford University
# Contact: Niels Joubert <niels@cs.stanford.edu>
#

import time, socket, sys, os, sys, inspect, signal, traceback
import json
import threading
from contextlib import closing
import collections
import binascii

from sbp.client.drivers.base_driver import BaseDriver
from sbp.client import Handler, Framer
from sbp.observation import SBP_MSG_OBS, SBP_MSG_BASE_POS_LLH, MsgObs
from sbp.navigation import SBP_MSG_POS_LLH, MsgPosLLH

import spooky, spooky.modules


class OdroidPersonCCModule(spooky.modules.SpookyModule):
  '''
  This is the Command and Control side of a remote OdroidPerson.
  '''

  def __init__(self, main, instance_name=None):
    spooky.modules.SpookyModule.__init__(self, main, "odroidperson_cc", instance_name=instance_name)
    self.bind_ip  = self.main.config.get_my('my-ip')
    self.cc_port  = self.main.config.get_foreign(instance_name, 'cc-server-port')
    self.cc_remote_port = self.main.config.get_foreign(instance_name, 'cc-local-port')
    self.cc_remote_ip   = self.main.config.get_foreign(instance_name, 'my-ip')
    self.cc_send_addr = (self.cc_remote_ip, self.cc_remote_port)

    self.last_heartbeat = 0
    self.last_payload = None
    self.send_id = 0

  def cmd_status(self):
    if self.last_heartbeat == 0:
      print self, "never received heartbeat message."
    else:
      print self, "last received message %.2fs ago: %s" % (time.time() - self.last_heartbeat, ", ".join(['{}: \"{}\"'.format(k,v) for k,v in self.last_payload.iteritems()]))


  def cc_ack(self, msg):
    print "ACK RECEIVED for %s" % msg['payload']

  def cc_nack(self, msg):
    print "NACK RECEIVED for %s" % msg['payload']

  def cc_heartbeat(self, msg):
    self.last_heartbeat = time.time()
    self.last_payload = msg['payload']
    if 'base-survey-status' in msg['payload']:
      if "Survey in progress" in msg['payload']['base-survey-status']:
        print self, msg['payload']['base-survey-status']
    return True

  def cc_simulator(self, msg):
    return False

  def cc_malformed(self, msg):
    print "CLIENT CLAIMS MALFORMED: %s" % msg['payload']
    return True

  def cc_unsupported(self, msg):
    print "CLIENT CLAIMS UNSUPPORTED: %s" % msg['payload']
    return True

  def cc_restarting(self, msg):
    print "CLIENT %s IS RESTARTING: %s" % (self.instance_name, msg)

  def send_cc(self, msgtype, payload=None):
    try:
      msg = {'msgtype':msgtype}
      if payload:
        msg['payload'] = payload
      msg['__ID__'] = self.send_id
      self.send_id += 1
      #print "sending message %s to %s, %s" % (msgtype, addr[0], addr[1])
      self.cc_udp.sendto(json.dumps(msg), self.cc_send_addr)
    except socket.error:
      traceback.print_exc()
      raise


  def handle_cc(self, cc_data, cc_addr):
    msg = json.loads(cc_data)

    msg_handler = {
      'ACK':         self.cc_ack,
      'NACK':        self.cc_nack,
      'heartbeat':   self.cc_heartbeat,
      'malformed':   self.cc_malformed,
      'unsupported': self.cc_unsupported,
      'restarting':  self.cc_restarting,       
    }

    if not 'msgtype' in msg:
      print "MALFORMED: message contains no \'msgtype'\ field."
      return

    if not msg['msgtype'] in msg_handler:
      print "UNSUPPORTED: message type \'%s\' not supported." % msg['msgtype']
      return
    
    success = msg_handler[msg['msgtype']](msg)
    if not ('ACK' in msg['msgtype'] or 'NACK' in msg['msgtype']):
      if success:
        self.send_cc('ACK', {'__ACK_ID__': msg['__ID__']})
      else:
        self.send_cc('NACK', {'__ACK_ID__': msg['__ID__']})
    return


  def enable_piksi_sim(self):
    self.send_cc('simulator', 'True')

  def disable_piksi_sim(self):
    self.send_cc('simulator', 'False')

  def reset_piksi(self):
    self.send_cc('reset_piksi')

  def cmd_shutdown(self):
    print 'Sending shutdown command to %s' % self.instance_name
    self.send_cc('shutdown')

  def cmd_restart(self):
    print 'Sending restart command to %s' % self.instance_name
    self.send_cc('restart')

  def cmd_update(self):
    print 'Sending update command to %s' % self.instance_name
    self.send_cc('update')

  def run(self):
    '''Thread loop here'''

    with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as cc_udp:
      cc_udp.setblocking(1)
      cc_udp.settimeout(0.1)
      cc_udp.bind((self.bind_ip, self.cc_port))
      self.cc_udp = cc_udp

      print "Module %s listening on %s" % (self, self.cc_port)
      
      self.ready()
      
      try:
        while True:
          if self.stopped():
            return

          try:
            cc_data, cc_addr = cc_udp.recvfrom(4096)
            self.handle_cc(cc_data, cc_addr)
          except (socket.error, socket.timeout) as e:
            pass

      except SystemExit:
        print "Exit Forced. We're dead."
        return

def init(main, instance_name=None):
  module = OdroidPersonCCModule(main, instance_name=instance_name)
  module.start()
  return module
