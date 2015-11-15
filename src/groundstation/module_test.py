import sys, os, time

import spooky, spooky.modules

class TestModule(spooky.modules.SpookyModule):
  '''
  This is a dumb test module
  '''

  def __init__(self, main):
    '''Create a UDP Broadcast socket'''
    spooky.modules.SpookyModule.__init__(self, "test", main)

  def run(self):
    '''Thread loop here'''
    while True:
      if self.stopped():
        return
      print "TestModule, here to annoy you!"
      time.sleep(1.0)

def init(main):
  module = TestModule(main)
  module.start()
  return module
