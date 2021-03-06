# Copyright (C) 2015 Stanford University
# Contact: Niels Joubert <niels@cs.stanford.edu>
#

import time, socket, sys, os, sys, inspect, signal, traceback
import threading, collections, Queue
from contextlib import closing

# Serialization Related
import cPickle as pickle
import json
import copy

# SPOOKY-related
import spooky, spooky.modules


'''
Example JSON-dumped System State
{
    "127.0.0.1": {
        "MsgBaselineNED": {
            "d": 0,
            "e": -91681,
            "flags": 1,
            "h_accuracy": 0,
            "n": -39933,
            "n_sats": 9,
            "tow": 88800,
            "v_accuracy": 0
        },
        "MsgDops": {
            "gdop": 180,
            "hdop": 160,
            "pdop": 190,
            "tdop": 170,
            "tow": 88200,
            "vdop": 150
        },
        "MsgGPSTime": {
            "flags": 0,
            "ns": 0,
            "tow": 88800,
            "wn": 1787
        },
        "MsgIarState": {
            "num_hyps": 1
        },
        "MsgPosLLH": {
            "flags": 1,
            "h_accuracy": 0,
            "height": 69.87143641833835,
            "lat": 37.42957419639449,
            "lon": -122.17414882947999,
            "n_sats": 9,
            "tow": 88800,
            "v_accuracy": 0
        },
        "MsgVelNED": {
            "d": 0,
            "e": 1495,
            "flags": 0,
            "h_accuracy": 0,
            "n": -3432,
            "n_sats": 9,
            "tow": 88800,
            "v_accuracy": 0
        },
        "_lastupdate": 1450386821.294973
    },
    "_timestamp": 1450386821.330007
}

'''
class SystemStateModule(spooky.modules.SpookyModule):
  '''
  THREAD-SAFE MODULE to CENTRALIZE SENSOR NETWORK "STATE VECTOR"
  Other modules stream state updates to this module in a thread-safe way,
  This module coordinates everything, producing a complete system state output

  *** THREAD-SAFETY: If you touch _state, FIRST AQUIRE _stateLock ***
  '''

  def __init__(self, main, instance_name=None):
    spooky.modules.SpookyModule.__init__(self, main, "systemstate", singleton=True)
    self._inputQueue = Queue.Queue()
    self._service_queue_period = self.main.config.get_my('state_service_queue_period')
    self._state_transmit_period = self.main.config.get_my('state_transmit_period')
    self._stateLock = threading.Lock()
    self._state = {}
    self.RECORDING = True
    dests = self.main.config.get_my('state_destinations')
    self.state_destinations = [(d[0], d[1]) for d in dests]

    self.piksi_baseline_max_age = 5.0 #Seconds

  # ===========================================================================
  # API and Internals for dealing with State
  # ===========================================================================

  def dump_state(self, filelike):
    pickle.dump(self.get_current(), filelike, pickle.HIGHEST_PROTOCOL)

  def get_current(self):
    self._stateLock.acquire()
    current_state = copy.copy(self._state)
    self._stateLock.release()
    current_state['_timestamp'] = time.time()
    return current_state

  def update_partial_state(self, node, iter):
    '''
    Push a new partial state update to the state vector.
    This assumes iter is of the form [(component, new_state),...]
    '''
    for component, new_state in iter: 
      self._inputQueue.put((node, component, new_state))

  def _handlePartialUpdate(self, item):
    '''INTERNAL: Handles a state up.'''
    self._stateLock.acquire()
    (node, component, new_state) = item
    if str(node) not in self._state:
      self._state[str(node)] = dict()
    self._state[str(node)][str(component)] = new_state
    self._state[str(node)]['_lastupdate'] = time.time()
    self._stateLock.release()

  def get_state_str(self):
    current = self.get_current()
    ret = "RECORDING = %s\n" % str(self.RECORDING)
    ret += json.dumps(current, sort_keys=True,
                        indent=4, separators=(',', ': '))
    return ret


  def get_piski_baseline_ned(self, piksi_ip):
    '''
    Returns the specified Piksi baseline, with human-readable status.
    [N,E,D] in units of mm

    '''
    piksi_ip = str(piksi_ip)
    state = self.get_current()
    
    if not piksi_ip in state:
      return (None, "No Piksi state available for %s" % piksi_ip)

    piksi_state = state[piksi_ip]

    if not 'MsgBaselineNED' in piksi_state:
      return (None, "No Piksi state available for %s" % piksi_ip)

    piksi_baseline = piksi_state['MsgBaselineNED']
    last_update = piksi_state['_lastupdate']
    age = time.time() - last_update
    ned = [piksi_baseline['n'], piksi_baseline['e'], piksi_baseline['d']]

    msg = None
    if spooky.testBit(piksi_baseline['flags'], 0) ==  0:
      msg = "Piksi only has Float baseline! flags=%d" %  piksi_baseline['flags']
      ned = None
    if age > self.piksi_baseline_max_age:
      msg = "Piksi Baseline older than %.2fs!" % self.piksi_baseline_max_age
      ned = None
    
    return (ned, msg)


  # ===========================================================================
  # Command line
  # ===========================================================================


  def cmd_status(self):
    print self, self.get_state_str()

  def cmd_record_start(self):
    print "Recording system state to %s" % self.log_filename
    self.RECORDING = True

  def cmd_record_stop(self):
    print "Pausing recording system state to %s" % self.log_filename 
    self.RECORDING = False

  def cmd_record_next(self):
    self.RECORDING = False
    print "NOT IMPLEMENTED YET."


  # ===========================================================================
  # Main Module Runloop and State Transmission
  # ===========================================================================

  def stop(self, quiet=False):
    self.main.unset_systemstate()
    super(SystemStateModule, self).stop(quiet=quiet)

  def send_state_as_json(self, dest_socket):
    try:
      if self.RECORDING:
        self.dump_state(self.f)

      data = json.dumps(self.get_current())
      for dest in self.state_destinations:
        try:
          n = dest_socket.sendto(data, dest)  
          if len(data) != n:
            print("%s State Output did not send all data!" % self)
          else:
            #print("%s State Output sent %d bytes" % (self,n))
            pass
        except socket.error:
          pass
    except:
      print self.get_current()
      import traceback
      traceback.print_exc()


  def run(self):
    try:
      self.main.set_systemstate(self)

      self.log_filename = spooky.find_next_log_filename(self.main.config.get_my("full-state-logs-prefix"))
      print "Opening logfile: %s" % self.log_filename 
      with open(self.log_filename, 'wb') as f:
        self.f = f

        with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as state_udp_out:
          state_udp_out.setblocking(1)
          state_udp_out.settimeout(0.05)

          print "Module %s sending data on %s" % (self, str(self.state_destinations))
    
          self.ready()

          send_state = spooky.DoPeriodically(lambda: self.send_state_as_json(state_udp_out), self._state_transmit_period)
          
          while not self.wait_on_stop(self._service_queue_period):
            while not self._inputQueue.empty():
              self._handlePartialUpdate(self._inputQueue.get_nowait())
            send_state.tick()

    except SystemExit:
      print "Exit Forced. We're dead."
    except:
      traceback.print_exc()
    finally:
      self.main.unset_systemstate()
      

def init(main, instance_name=None):
  module = SystemStateModule(main, instance_name=instance_name)
  module.start()
  return module
