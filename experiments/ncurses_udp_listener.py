# Copyright (C) 2015 Stanford University
# Contact: Niels Joubert <niels@cs.stanford.edu>
#



#Globally useful stuff
import time, socket, sys, os, sys, inspect, traceback
import argparse, json, binascii
import struct
import logging

#Threading-related:
import threading, Queue
from contextlib import closing
import curses

def main(ip, port, buffer, decodeJSON=True):
  stdscr = curses.initscr()
  curses.noecho()
  curses.cbreak()

  begin_x = 0; begin_y = 0
  height = 300; width = 300
  win = curses.newwin(height, width, begin_y, begin_x)

  #pad = curses.newpad(100, 100)

  #  Displays a section of the pad in the middle of the screen
  
  with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as udp:

    try:
      udp.setblocking(1)
      udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
      udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
      udp.bind((ip, port))

      win.addstr(0,0, "Listening on udp:%s:%s\n" % (ip, port))
      win.refresh()
      while True:
        data, addr = udp.recvfrom(buffer)
        if decodeJSON:
          jd = json.loads(data)
          #jd = { '51': { 'NED': jd['192.168.2.51']['MsgBaselineNED'], 'YAW': jd['192.168.2.51']['ATTITUDE'] }, '52': { 'NED': jd['192.168.2.52']['MsgBaselineNED'], 'YAW': jd['192.168.2.52']['ATTITUDE'] }}
          data = json.dumps(jd, sort_keys=True, indent=4, separators=(',', ': '))
        dl = data.split("\n")
        for y in range(1, len(dl)+1):
            try:
              win.addstr(y,0, dl[y-1])
            except curses.error:
              pass
        win.refresh()
    except KeyboardInterrupt:
        return 
    finally:
      print "EXITING"
      curses.nocbreak()
      curses.echo()
      curses.endwin()


if __name__ == "__main__":

  parser = argparse.ArgumentParser(description="Simple UDP listener")
  parser.add_argument("-i", "--ip",
                      default=['127.0.0.1'], nargs=1,
                      help="Bind to this IP")
  parser.add_argument("-p", "--port", 
                      default=[19000], nargs=1,
                      help="Bind to this port")
  parser.add_argument("-b", "--buffer",
                      default=[8096], nargs=1,
                      help="Size of UDP buffer in bytes")
  args = parser.parse_args()

  main(args.ip[0], int(args.port[0]), int(args.buffer[0]))