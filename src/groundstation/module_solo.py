# Copyright (C) 2015 Stanford University
# Contact: Niels Joubert <niels@cs.stanford.edu>
#

# General
import time, socket, sys, os, sys, inspect, signal, traceback
import json
import threading
from contextlib import closing
import collections
import binascii

# 3DR Solo-related
import dronekit
from pymavlink import mavutil # Needed for command message definitions

# Spooky
import spooky, spooky.modules, spooky.coords

class SoloModule(spooky.modules.SpookyModule):
  '''
  Module connects:
    1) Camera API over UDP; to
    2) DroneKit API to fly the drone around

  Input API:

  { 
    msgtype: 'GOTO',
    payload: {
      position: [0,0,0],
      positiondot: [0,0,0],
      lookat: [0,0,0],
      lookatdot: [0,0,0],
      gimbal: [0,0,0],
      gimbaldot: [0,0,0]
    }
  }

  Semantics:

    - coord system: defined by Piksi NED.
    - If you want the quad to hold position, DO NOT SEND A VELOCITY.
    - You can send whatever combination of messages. lookat is priotitized over gimbal.

  State we send to "systemstate":

  {
    mode
    position
    velocity
    gimbal deets...
  }
  '''

  def __init__(self, main, instance_name=None):
    spooky.modules.SpookyModule.__init__(self, main, "solo", singleton=True)
    self.bind_ip  = self.main.config.get_my('listen-on-ip')
    self.bind_port = self.main.config.get_my('camera_cc_port')
    self.dronekit_device = self.main.config.get_my('dronekit-device')
    self.streamrate = self.main.config.get_my('dronekit-streamrate')
    self.groundspeed = self.main.config.get_my('dronekit-groundspeed')
    self.airspeed = self.main.config.get_my('dronekit-airspeed')
    
    main.add_command('solo', self.cmd_solo, 'send commands to the 3DR Solo (if connected)')

    # NJ HACK SIGGRAPH 2016: USB Port broke off
    self.base_station_llh = [37.4312111012, -122.1751602, 25]
    self.spooky_vehicle_fake_ned = [0,0,0]
    self.spooky_vehicle_fake_fix_type = 0
    self.spooky_vehicle_fake_nsats = 0

    self.vehicle_hb_threashold = 5.0
    self._ENABLE_API = False
    self.statusmsg_API = ""
    self.last_api_msg = None

    self.vehicle = None
    self.vehicle_home_ned = None #[-2656, -2131, 2256] #None
    self.vehicle_home = None
    self.alt_comp_up = 0
    self.north_comp = 0
    self.east_comp = 0

    self.down_from_piksi_to_camera = 100 #mm from piksi to camera. EG: Camera as 100mm down from piksi.

    self._executor = None
    self.init_executor()

    self.cleared_to_execute = threading.Event()
    self.cleared_to_execute.set()
    self.okay = False

    self.last_gps_obs_inject = 0


  def init_executor(self):
    if self._executor:
      self._executor.stop()
      # TODO: Potentially CRASH the executor thread here.
    self._executor = spooky.ExecutorThread()
    self._executor.start()


  def stop(self, quiet=False):
    self.MAYDAY_stop_solo()
    self.disconnect()
    self.main.del_command('solo')
    super(SoloModule, self).stop(quiet=quiet)

  # ===========================================================================
  # INTERNALS
  # ==========================================================================

  def _spooky_to_vehicle_llh_relative(self, ned):
    '''
    Converts spooky-frame integer mm NED value
    to Drone Global Coordinate Frame with Relative altitude in meters
    '''
    if not self.vehicle_home:
      print "FAILED _spooky_to_vehicle_llh_relative: No vehicle home!"
      return None
    drone_ned = self._spooky_to_vehicle_ned(ned)
    home_llh = [self.vehicle_home.lat, self.vehicle_home.lon, 0]
    return spooky.coords.ned2llh(drone_ned, home_llh)

  def _vehicle_llh_relative_to_spooky_ned(self, llh):
    '''
    Converts Drone Global Coordinate Frame with Relative altitude
    to spooky-frame integer mm NED value
    '''
    if not self.vehicle_home:
      print "FAILED _vehicle_llh_relative_to_spooky_ned: No vehicle home!"
      return None

    home_llh = [self.vehicle_home.lat, self.vehicle_home.lon, 0]
    drone_ned = spooky.coords.llh2ned(llh, home_llh)
    return self._vehicle_to_spooky_ned(drone_ned)

  def _spooky_to_vehicle_ned(self, ned):
    '''
    Converts spooky-frame integer mm NED value
    to Drone-frame float meter NED value
    '''
    if self.vehicle_home_ned is None:
      print "_spooky_to_vehicle_ned with no vehicle_home_ned"
      return [
        float(ned[0] + self.north_comp)/1000.0, 
        float(ned[1] + self.east_comp)/1000.0, 
        float(ned[2] - self.alt_comp_up)/1000.0
        ] 
    else:
      return [
        float(ned[0] - self.vehicle_home_ned[0] + self.north_comp)/1000.0, 
        float(ned[1] - self.vehicle_home_ned[1] + self.east_comp)/1000.0, 
        float(ned[2] - self.vehicle_home_ned[2] - self.alt_comp_up)/1000.0
        ]

  def _vehicle_to_spooky_ned(self, ned):
    '''
    Converts a Drone-frame float meter NED value
    to a spooky-frame integer mm NED value
    '''    
    if self.vehicle_home_ned is None:
      print "_vehicle_to_spooky_ned with no vehicle_home_ned"
      return [
        int(ned[0] * 1000.0) - self.north_comp,
        int(ned[1] * 1000.0) - self.east_comp,
        int(ned[2] * 1000.0) + self.alt_comp_up # TODO: Do we actually want this?
      ]
    else:
      return [
        int(ned[0]*1000.0) + self.vehicle_home_ned[0] - self.north_comp, 
        int(ned[1]*1000.0) + self.vehicle_home_ned[1] - self.east_comp, 
        int(ned[2]*1000.0) + self.vehicle_home_ned[2] + self.alt_comp_up # TODO: Do we actually want this?
      ]

  def _spooky_to_vehicle_vel(self, vel):
    '''
    Converts a spooky-frame integer mm velocity value (NED)
    to a drone-frame float meter velocity value (NED)
    '''
    return [
      float(vel[0])/1000.0,
      float(vel[1])/1000.0,
      float(vel[2])/1000.0
    ]

  # ===========================================================================
  # 3DR SOLO DRONEKIT INTERFACE
  # ===========================================================================

  def update_spooky_ned_from_mavlink(self, llh, fix_type, nsats):

    # get NED between base and given LLH
    ned = spooky.coords.llh2ned(llh, self.base_station_llh)*1000.0

    self.spooky_vehicle_fake_ned = ned
    if (self.spooky_vehicle_fake_fix_type != fix_type):
      print "SBP FIX TYPE CHANGING: NOW IT IS", fix_type
    self.spooky_vehicle_fake_fix_type = fix_type
    self.spooky_vehicle_fake_nsats = nsats
    
    flags = 0
    if fix_type == 5:
      flags = 1
    spooky_ned = {
      "n": ned[0],
      "e": ned[1],
      "d": ned[2],
      "flags": flags
    }

    self.main.modules.trigger('update_partial_state', 'solo_fake_sbp', [('ned', spooky_ned)])

  def callback_gps_0(self, vehicle, attr_name, value):

    if value.fix_type >= 3:
      self.update_spooky_ned_from_mavlink([value.lat, value.lon, value.alt], value.fix_type, value.satellites_visible);

  def callback_location(self, vehicle, attr_name, value):
    try:
      location = value.global_relative_frame
      if location is None or (location.lat == 0.0 and location.lon == 0.0):
        return
      llh = {
        'lat'  :location.lat,
        'lon'  :location.lon,
        'alt'  :location.alt,
        'coord': 'LocationGlobalRelative'
      }

      loc = [location.lat, location.lon, location.alt]
      spooky_ned = self._vehicle_llh_relative_to_spooky_ned(loc)
      self.main.modules.trigger('update_partial_state', 'solo', [('lookFrom', spooky_ned), (attr_name, llh)])
    except:
      import traceback
      traceback.print_exc()

  def callback_attitude(self, vehicle, attr_name, value):
    attitude = {
      'pitch': value.pitch,
      'yaw': value.yaw,
      'roll': value.roll
    }
    self.main.modules.trigger('update_partial_state', 'solo', [(attr_name, attitude)])

  def callback_mount(self, vehicle, attr_name, value):
    attitude = {
      'pitch': value[0],
      'yaw': value[1],
      'roll': value[2]
    }
    self.main.modules.trigger('update_partial_state', 'solo', [(attr_name, value)])


  def ahrs_reset_home(self):

    msg = self.vehicle.message_factory.command_long_encode(
      0, # target system
      0, # target component
      mavutil.mavlink.MAV_CMD_DO_SET_HOME, # command
      0,            # confirmation
      1,            # param 1
      0,            # param 2
      0,            # param 3
      0,            # param 4
      0,            # param 5
      0,            # param 6
      0             # param 7
      )

    # send command to vehicle
    self.vehicle.send_mavlink(msg)

    self._update_vehicle_home()

  def _request_home_position(self):
    '''
    Sends a mavlink command to request the home location.
    Relies on the existence of a listener for the HOME_POSITION message.
    '''
    msg = self.vehicle.message_factory.command_long_encode(
      0, # target system
      0, # target component
      mavutil.mavlink.MAV_CMD_GET_HOME_POSITION, # command
      0,            # confirmation
      2,            # param 1
      0,            # param 2
      0,            # param 3
      0,            # param 4
      0,            # param 5
      0,            # param 6
      0             # param 7
      )

    # send command to vehicle
    self.vehicle.send_mavlink(msg)

    print "Requested home location."

  def _update_vehicle_home(self):

    self._request_home_position()

    ### OLD CODE: WE USED TO RELY ON DRONEKIT LOCATION, BUT THIS IS ONLY FLOAT ACCURATE!

    # '''Internal call to update our vehicle home location'''
    # if not self.vehicle:
    #   print "_update_vehicle_home: no vehicle attached"
    #   return

    # cmds = self.vehicle.commands
    # cmds.wait_ready()
    # cmds.download()
    # cmds.wait_ready()

    # self.vehicle_home = self.vehicle.home_location

    # if self.vehicle_home is not None:
    #   home_location_llh = {
    #     'lat'  : self.vehicle_home.lat,
    #     'lon'  : self.vehicle_home.lon,
    #     'alt'  : self.vehicle_home.alt,
    #     'coord': 'LocationGlobalRelative'
    #   }
    #   self.main.modules.trigger('update_partial_state', 'solo', [('home_location', home_location_llh)])
    # print "Downloading latest HOME location:", str(self.vehicle_home)

  def connect(self):
    self.disconnect()

    # Configure our SoloLink connection
    def deferred():
      print 'Connecting to vehicle on: %s' % self.dronekit_device
      self.vehicle = dronekit.connect(self.dronekit_device, wait_ready=True, rate=self.streamrate, heartbeat_timeout=10)
      
      print "Vehicle Connected! Setting airpspeed and downloading commands..."
      
      self.vehicle.groundspeed = self.groundspeed # Make it move SLOWLY
      self.vehicle.airspeed = self.airspeed

      self._update_vehicle_home()

      self.vehicle.add_attribute_listener('location', self.callback_location)
      self.vehicle.add_attribute_listener('attitude', self.callback_attitude)
      self.vehicle.add_attribute_listener('mount', self.callback_mount)

      spooky_solo = self

      @self.vehicle.on_message('HOME_POSITION')
      def listener(self, name, msg):

        if msg.latitude == 0 and msg.longitude == 0:
          print "Received latest HOME location, BUT ITS BLANK. Setting to Zero"
          spooky_solo.vehicle_home = None
          return
        
        home_location = dronekit.LocationGlobal(msg.latitude / 1.0e7, msg.longitude / 1.0e7, msg.altitude / 100.0)

        spooky_solo.vehicle_home = home_location

        home_location_llh = {
          'lat'  : spooky_solo.vehicle_home.lat,
          'lon'  : spooky_solo.vehicle_home.lon,
          'alt'  : spooky_solo.vehicle_home.alt,
          'coord': 'LocationGlobal'
        }
        spooky_solo.main.modules.trigger('update_partial_state', 'solo', [('home_location', home_location_llh)])
        
        print "Received latest HOME location:", str(spooky_solo.vehicle_home)

      @self.vehicle.on_message('GOPRO_SET_RESPONSE')
      def listener(self, name, msg):
        print name
        print msg

      @self.vehicle.on_message('GOPRO_GET_RESPONSE')
      def listener(self, name, msg):
        print name
        print msg

      self.set_piksi_home()


      print "Vehicle Ready!"

    self._executor.execute(deferred)

  def disconnect(self):
    if self.vehicle:
      self.vehicle.close()
      print "Vehicle Disconnected!" 
    self.vehicle = None
    self.vehicle_home = None
    
  def MAYDAY_stop_solo(self):
    if self.vehicle:
      self.vehicle.mode = dronekit.VehicleMode("LOITER")
    self.disable_API()
    self.init_executor()
    if self.vehicle:
      self.vehicle.mode = dronekit.VehicleMode("LOITER")

  def check_vehicle(self):
    ''' We run this periodically in the thread runloop.'''
    if not self.vehicle:
      return
    if self.vehicle.last_heartbeat > self.vehicle_hb_threashold:
      print "SOLO LINK COMPROMISED: Last Heartbeat %.2fs ago" % self.vehicle.last_heartbeat
      print "STOPPING EVERYTHING!"
      self.MAYDAY_stop_solo()

  def enable_API(self):
    print "ENABLING CAMERA API"
    #self._update_vehicle_home()
    self._ENABLE_API = True

  def disable_API(self):
    print "DISABLING CAMERA API"
    self._ENABLE_API = False

  def arm(self, value=True, timeout=2.0):

    def deferred():
      if not self.vehicle:
        print "No vehicle attached. "
        return False

      if value:
        if (not self.vehicle.armed and not self.vehicle.is_armable):
          print " The vehicle is not ready to arm: %s, %s" % (self.vehicle, self.vehicle.is_armable)
          return False

        print "Arming into GUIDED mode..."
        self.vehicle.armed   = True
        armingWait = spooky.BusyWaitWithTimeout(lambda: self.vehicle.armed, 0.1, 5.0)
        if not armingWait.wait():
          print "Vehicle didn't arm in the allotted time..."
          self.vehicle.armed = False
          return False
        else:
          self._update_vehicle_home()
          self.vehicle.mode    = dronekit.VehicleMode("GUIDED")
          return True
      else:
        print "Disarming"
        self.vehicle.armed   = False

    self._executor.execute(deferred)
    
  def takeoff(self, aTargetAltitude=5.0):

    def deferred():
      if not self.vehicle.armed:
        print "Vehicle is not armed"
        return False

      print "Taking off!"
      self.vehicle.simple_takeoff(aTargetAltitude) # Take off to target altitude

      # Wait until the self.vehicle reaches a safe height before processing the goto (otherwise the command 
      #  after Vehicle.simple_takeoff will execute immediately).
      while True:
          print " Altitude: ", self.vehicle.location.global_relative_frame.alt      
          if self.vehicle.location.global_relative_frame.alt>=aTargetAltitude*0.95: #Trigger just below target alt.
              print "Reached target altitude"
              break
          time.sleep(1)

    self._executor.execute(deferred)

  def land(self):
    self._executor.cancel()
    self.vehicle.mode    = dronekit.VehicleMode("Land")

  def rtl(self):
    self._executor.cancel()
    self.vehicle.mode    = dronekit.VehicleMode("RTL")


  def sendCameraOrientation(self, rpy):
    '''
    RPY in DEGREES...
    While Attitude is reported in Radians
    '''
    if not self.vehicle:
      return False

    print "Sending cameraorientation RPY", rpy

    msg = self.vehicle.message_factory.mount_configure_encode(
                0, 1,    # target system, target component
                mavutil.mavlink.MAV_MOUNT_MODE_MAVLINK_TARGETING,  #mount_mode
                1,  # stabilize roll
                1,  # stabilize pitch
                1,  # stabilize yaw
                )
    self.vehicle.send_mavlink(msg)
    msg = self.vehicle.message_factory.mount_control_encode(
                0, 1,    # target system, target component
                rpy[1] * 100, # pitch is in centidegrees
                rpy[0] * 100, # roll
                rpy[2] * 100, # yaw is in centidegrees
                0) # save position
    self.vehicle.send_mavlink(msg)

  def sendLookAtSpookyNED_simple(self, ned, vel=[0,0,0]):
    if not self.vehicle:
      return False
    
    print "sendLookAtSpookyNED_simple", ned
    llh = self._spooky_to_vehicle_llh_relative(ned)
    if llh is None:
      print "Can't look-at, no home location!"
      return

    # ==================================================
    # The old, inaccurate DO_SET_ROI call
    # ==================================================

    msg = self.vehicle.message_factory.command_long_encode(
                                                    1, 1,    # target system, target component
                                                    mavutil.mavlink.MAV_CMD_DO_SET_ROI, #command
                                                    0, #confirmation
                                                    0, 0, 0, 0, #params 1-4
                                                    llh[0],
                                                    llh[1],
                                                    llh[2]
                                                    )
    self.vehicle.send_mavlink(msg)

  def sendLookAtSpookyNED(self, ned, vel=[0,0,0]):
    if not self.vehicle:
      return False
    
    print "sendLookAtSpookyNED", ned
    llh = self._spooky_to_vehicle_llh_relative(ned)
    if llh is None:
      print "Can't look-at, no home location!"
      return

    # ==================================================
    # The new, accurate set_position_target_global_int_encode call
    # ==================================================

    msg = self.vehicle.message_factory.set_roi_global_int_encode(0, #time_boot_ms
        1, #target_system
        1, #target_component
        mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
        0, #type_mask
        0, #roi_index
        0, #timeout_ms
        int(llh[0]*1e7), #lat int
        int(llh[1]*1e7), #lng int
        float(llh[2]),   #alt
        float(vel[0]), #vx
        float(vel[1]), #vy
        float(vel[2]), #vz
        0, #ax
        0, #ay
        0) #az

    self.vehicle.send_mavlink(msg)

  def sendLookAtSpookyNED_COMMAND_INT(self, ned, vel=[0,0,0]):
    if not self.vehicle:
      return False

    print "sendLookAtSpookyNED_COMMAND_INT", ned
    llh = self._spooky_to_vehicle_llh_relative(ned)
    if llh is None:
      print "Can't look-at, no home location!"
      return

    # ==================================================
    # The new, standard, accurate DO_SET_ROI call
    # ==================================================

    msg = self.vehicle.message_factory.command_int_encode(
                                                    1, 1,    # target system, target component
                                                    mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                                                    mavutil.mavlink.MAV_CMD_DO_SET_ROI, #command
                                                    0, #current
                                                    0, #autocontinue
                                                    0, 0, 0, 0, #params 1-4
                                                    int(llh[0] * 1e7),
                                                    int(llh[1] * 1e7),
                                                    llh[2]
                                                    )
    self.vehicle.send_mavlink(msg)


  def sendLookFromSpookyNED_simple(self, ned, vel=[0,0,0], useVel=False):
    if not self.vehicle:
      return False

    drone_llh = self._spooky_to_vehicle_llh_relative(ned)
    print "sendLookFromSpookyNED_simple ned=", ned, "drone_llh=", drone_llh, "useVel=", useVel, "vel=", vel

    point1 = dronekit.LocationGlobalRelative(drone_llh[0], drone_llh[1], drone_llh[2])
    self.vehicle.simple_goto(point1)



  def sendLookFromSpookyNED(self, ned, vel=[0,0,0], useVel=False):
    '''
    Assumes NED is in "mm" integers in our spooky coordinate frame.

    https://pixhawk.ethz.ch/mavlink/#SET_POSITION_TARGET_GLOBAL_INT

    '''
    drone_ned = self._spooky_to_vehicle_ned(ned)
    drone_llh = self._spooky_to_vehicle_llh_relative(ned)
    print "sendLookFromSpookyNED ned=", ned, "drone_ned=", drone_ned, "drone_llh=", drone_llh, "useVel=", useVel, "vel=", vel

    if not self.vehicle:
      return False

    if (-1*drone_ned[2]) < 0.5:
      print "LookFrom less than 1m, cowarding out..."
      return

    # https://pixhawk.ethz.ch/mavlink/#SET_POSITION_TARGET_GLOBAL_INT

    ignoremask = 3520 #ignore yaw stuff, and accel stuff 
    if not useVel:
      ignoremask = ignoremask | 56
    msg = self.vehicle.message_factory.set_position_target_global_int_encode(
        0,  # system time in ms
        1,  # target system
        0,  # target component
        mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
        ignoremask, # ignore
        int(drone_llh[0] * 1e7),
        int(drone_llh[1] * 1e7),
        drone_llh[2],
        vel[0], vel[1], vel[2], # velocity
        0, 0, 0, # accel x,y,z
        0, 0) # yaw, yaw rate

    self.vehicle.send_mavlink(msg)

    return True

  def sendLookFromSpookyNED_local_frame(self, ned, vel=[0,0,0], useVel=False):
    '''
    Assumed NED is in "mm" integers in our spooky coordinate frame.

    https://pixhawk.ethz.ch/mavlink/#SET_POSITION_TARGET_LOCAL_NED
    '''
    drone_ned = self._spooky_to_vehicle_ned(ned)

    print "sendLookFromSpookyNED_local_frame ned=", ned, "drone_ned=", drone_ned 

    if not self.vehicle:
      return False

    if (-1*drone_ned[2]) < 0.5:
      print "LookFrom less than 1m, cowarding out..."
      return

    ignoremask = 3520 #ignore yaw stuff, and accel stuff 
    if not useVel:
      ignoremask = ignoremask | 56

    # N - in m
    # E - in m
    # D - in m
    msg = self.vehicle.message_factory.set_position_target_local_ned_encode(
        0,  # system time in ms
        1,  # target system
        0,  # target component
        mavutil.mavlink.MAV_FRAME_LOCAL_NED,
        ignoremask, # ignore
        drone_ned[0],
        drone_ned[1],
        drone_ned[2],
        vel[0], vel[1], vel[2], # velocity
        0, 0, 0, # accel x,y,z
        0, 0) # yaw, yaw rate

    self.vehicle.send_mavlink(msg)

    return True

    
  def gopro_cmd(self, args):
    print args

    if not self.vehicle:
      print "No vehicle connected"
      return

    if args[1] == "off":

      msg = self.vehicle.message_factory.gopro_set_request_encode(
        0,
        mavutil.mavlink.MAV_COMP_ID_GIMBAL,
        mavutil.mavlink.GOPRO_COMMAND_POWER,
        [0,0,0,0]
        )

      self.vehicle.send_mavlink(msg)

    if args[1] == "on":

      msg = self.vehicle.message_factory.gopro_set_request_encode(
        0,
        mavutil.mavlink.MAV_COMP_ID_GIMBAL,
        mavutil.mavlink.GOPRO_COMMAND_POWER,
        [1,1,1,1]
        )

      self.vehicle.send_mavlink(msg)

    if args[1] == "start":
      print "Starting gopro"

      msg = self.vehicle.message_factory.gopro_set_request_encode(
        0,
        mavutil.mavlink.MAV_COMP_ID_GIMBAL,
        mavutil.mavlink.GOPRO_COMMAND_SHUTTER,
        [1,1,1,1]
        )

      self.vehicle.send_mavlink(msg)

    if args[1] == "stop":
      print "Starting gopro"

      msg = self.vehicle.message_factory.gopro_set_request_encode(
        0,
        mavutil.mavlink.MAV_COMP_ID_GIMBAL,
        mavutil.mavlink.GOPRO_COMMAND_SHUTTER,
        [0,0,0,0]
        )

      self.vehicle.send_mavlink(msg)

    if args[1] == "info":
      print "Requesting GOPRO info"

      print "  COMMAND_MODEL = ", mavutil.mavlink.GOPRO_COMMAND_MODEL
      msg = self.vehicle.message_factory.gopro_get_request_encode(
        0,
        mavutil.mavlink.MAV_COMP_ID_GIMBAL,
        mavutil.mavlink.GOPRO_COMMAND_MODEL
        )

      self.vehicle.send_mavlink(msg)

      print "  COMMAND_VIDEO_SETTINGS = ", mavutil.mavlink.GOPRO_COMMAND_VIDEO_SETTINGS
      msg = self.vehicle.message_factory.gopro_get_request_encode(
        0,
        mavutil.mavlink.MAV_COMP_ID_GIMBAL,
        mavutil.mavlink.GOPRO_COMMAND_VIDEO_SETTINGS
        )

      self.vehicle.send_mavlink(msg)


    if args[1] == "fov":
      if len(args) < 3:
        print "specify wide|medium|narrow"
        return

      fov_mapping = {
        "wide": mavutil.mavlink.GOPRO_FIELD_OF_VIEW_WIDE,
        "medium": mavutil.mavlink.GOPRO_FIELD_OF_VIEW_MEDIUM,
        "narrow": mavutil.mavlink.GOPRO_FIELD_OF_VIEW_NARROW,
      }
      if not args[2] in fov_mapping:
        print "specify wide|medium|narrow"
        return
   
      msg = self.vehicle.message_factory.gopro_set_request_encode(
        0,
        mavutil.mavlink.MAV_COMP_ID_GIMBAL,
        mavutil.mavlink.GOPRO_COMMAND_VIDEO_SETTINGS,
        [3,7,fov_mapping[args[2]]  ,0]
        )

      self.vehicle.send_mavlink(msg)



  # def injectGPS(self, sbpPacket):

  #   self.last_gps_obs_inject = time.time()

  #   if not self.vehicle:
  #     return

  #   length = len(sbpPacket)
  #   data = bytearray(sbpPacket.ljust(110))

  #   if length > 110:
  #     print "SOLO ERROR: injectGPS attempting to send packet larger than 110 bytes (%d bytes)" % length

  #   msg = self.vehicle.message_factory.gps_inject_data_encode(
  #     0, #target_system
  #     0, #target_component,
  #     length,
  #     data)

  #   self.vehicle.send_mavlink(msg)

  # ===========================================================================
  # UDP-BASED CAMERA API
  # ===========================================================================

  def camapi_goto(self, msg, prompt=False):
    if not 'payload' in msg:
      return False

    if not self._ENABLE_API:
      self.statusmsg_API = "Received msg %s but camera API not enabled." % msg['msgtype']
      return

    if prompt:
      if not self.cleared_to_execute.isSet():
        return

      self.okay = False
      self.cleared_to_execute.clear()

    msg = msg['payload']
    
    self.main.modules.trigger('update_partial_state', 'CAM_API', [('shotName', msg['shotName'])])

    self.last_api_msg = msg

    desiredstate = msg['desiredstate']
    # Unpack payload

    def deferred():
      try:
        if prompt:
          print ""
          print "API CALL: Flying solo to %s looking at %s" % (str(desiredstate['position']), str(desiredstate['lookat']))
          print "This sticks the solo at %s" % str(self._spooky_to_vehicle_ned(desiredstate['position']))
          print "Please type solo <ok>|<no> depending on whether you're okay with this!"
          self.cleared_to_execute.wait()
        if not prompt or self.okay:
          useVel = desiredstate['usevel'] == 'true'
          self.sendLookFromSpookyNED(desiredstate['position'], vel=self._spooky_to_vehicle_vel(desiredstate['positiondot']), useVel=useVel)
          if desiredstate['orientationtype'] == 'lookat':
            self.sendLookAtSpookyNED_COMMAND_INT(desiredstate['lookat'])          
          else:
            self.sendCameraOrientation(desiredstate['gimbal'])
        else:
          print "NOPE!"
      except:
          traceback.print_exc()

    self._executor.execute(deferred)

    '''
    {u'shotName': u'Reverse: 1', u'shotDuration': 4.0, u'desiredstate': {u'positiondot': [0.0, 0.0, 0.0], u'gimbal': [0.0, 0.0, 4.18879032], u'gimbaldot': [0.0, 0.0, 0.0], u'coord': u'PiksiNED', u'lookat': [0.0, 0.0, -1272.421], u'lookatdot': [0.0, 0.0, 0.0], u'position': [653.4773, 1131.85583, -1272.421]}}
    '''

    return True

  def handle_camapi(self, data, addr):
    msg = json.loads(data)

    msg_handler = {
      'CAMAPI_GOTO':  self.camapi_goto,       
    }

    if not 'msgtype' in msg:
      print "MALFORMED: message contains no \'msgtype\' field."
      return

    if not msg['msgtype'] in msg_handler:
      print "UNSUPPORTED: message type \'%s\' not supported." % msg['msgtype']
      return
    
    success = msg_handler[msg['msgtype']](msg)

    #if not success:
    #  print "FAILED: message type \'%s\' failed to execute properly" % msg['msgtype']


  # ===========================================================================
  # Command Line Access
  # ===========================================================================

  def cmd_status(self):

    print "*** SOLO STATUS ***"
    print "  SBP Observation Injection"
    if self.last_gps_obs_inject == 0:
      print "    No SBP Observation Injection has occurred yet"
    else:
      print "    Last SBP Observation Injection %.2fs ago" % (time.time() - self.last_gps_obs_inject)
    print "    Vehicle FAKE NED from Mavlink: ", self.spooky_vehicle_fake_ned, " fix type ", self.spooky_vehicle_fake_fix_type, " nsats ", self.spooky_vehicle_fake_nsats
    print "  VEHICLE CONNECTED?", self.vehicle != None
    print "    piksi NED home pos:", self.vehicle_home_ned
    print "  SOLO API ENABLED?", self._ENABLE_API
    print "    Solo API last msg:", self.statusmsg_API
    exStatus = self._executor.status()
    print "  Executor: Executing? %s Commands in Queue? %d" % (str(exStatus[0]), exStatus[0])
    if self.vehicle:
      print "  Vehicle state:"
      print "    System Status: %s" % self.vehicle.system_status
      print "    Home Location: %s" % self.vehicle.home_location
      print "    Global Location: %s" % self.vehicle.location.global_frame
      print "    Global Location (relative altitude): %s" % self.vehicle.location.global_relative_frame
      print "    Local Location: %s" % self.vehicle.location.local_frame
      print "    Attitude: %s" % self.vehicle.attitude
      print "    Velocity: %s" % self.vehicle.velocity
      print "    Battery: %s" % self.vehicle.battery
      print "    Last Heartbeat: %s" % self.vehicle.last_heartbeat
      print "    Heading: %s" % self.vehicle.heading
      print "    Groundspeed: %s" % self.vehicle.groundspeed
      print "    Airspeed: %s" % self.vehicle.airspeed
      print "    Mode: %s" % self.vehicle.mode.name
      print "    Is Armable?: %s" % self.vehicle.is_armable
      print "    Armed: %s" % self.vehicle.armed
    if self.last_api_msg:
      print "Last API message:", self.last_api_msg
    else:
      print "No API messages received yet"

  def cmd_goto(self, n, e, d, simple=False, local=False):
    print "Command to go to (%d,%d,%d) in mm ned spooky frame." % (n, e, d)
    if simple:
      print "Command Result: %s" % self.sendLookFromSpookyNED_simple([n,e,d])
    elif local:
      print "Command Result: %s" % self.sendLookFromSpookyNED_local_frame([n,e,d])
    else:
      print "Command Result: %s" % self.sendLookFromSpookyNED([n,e,d])

  def cmd_lookat(self, n, e, d, vel=[0,0,0], simple=False):
    print "Command to look at (%d,%d,%d) in mm ned spooky frame with vel=%s" % (n, e, d, str(vel))
    if simple:
      print "Command Result: %s" % self.sendLookAtSpookyNED_simple([n,e,d])
    else:
      print "Command Result: %s" % self.sendLookAtSpookyNED([n,e,d],vel=vel)

  def cmd_orientation(self, r, p, y):
    print "Command to orient with (%f,%f,%f)" % (r, p, y)
    self.sendCameraOrientation([r, p, y])

  def cmd_solo(self, args):

    def usage():
      print "solo (mayday|status|connect|disconnect|arm|disarm|takeoff|goto[_simple|_local] <n> <e> <d>|lookat <n> <e> <d>|orient <r> <p> <y>|rtl|go|stop|set_home (<piksi_ip>|ahrs_reset_home|<n mm> <e mm> <d mm>)|ok|no|up <mm>|north <mm>|east <mm>|set_alt_comp <mm>)"
      print args

    if 'mayday' in args:
      return self.MAYDAY_stop_solo()
    elif 'status' in args:
      return self.cmd_status()
    elif 'gopro' in args:
      if len(args) == 1:
        print "solo gopro <start|stop|info|fov [wide|medium|narrow]>"
        return
      return self.gopro_cmd(args)
    elif 'connect' in args:
      return self.connect()
    elif 'disconnect' in args:
      return self.disconnect()
    elif 'arm' in args:
      self.arm()
      return
    elif 'disarm' in args:
      self.arm(value=False)
      return
    elif 'takeoff' in args:
      self.takeoff()
      return
    elif 'rtl' in args:
      self.rtl()
      return
    elif 'go' in args:
      return self.enable_API()
    elif 'stop' in args:
      return self.disable_API()
    elif 'goto' in args:
      if len(args) < 4:
        return usage()
      return self.cmd_goto(int(args[1]),int(args[2]),int(args[3]))
    elif 'goto_simple' in args:
      if len(args) < 4:
        return usage()
      return self.cmd_goto(int(args[1]),int(args[2]),int(args[3]), simple=True)
    elif 'goto_local' in args:
      if len(args) < 4:
        return usage()
      return self.cmd_goto(int(args[1]),int(args[2]),int(args[3]), local=True)
    elif 'lookat' in args:
      if len(args) == 4:
        return self.cmd_lookat(int(args[1]),int(args[2]),int(args[3]))
      elif len(args) == 7: 
        return self.cmd_lookat(int(args[1]),int(args[2]),int(args[3]), vel=[int(args[4]), int(args[5]), int(args[6])])
      else:
        return usage()
    elif 'lookat_simple' in args:
      if len(args) == 4:
        return self.cmd_lookat(int(args[1]),int(args[2]),int(args[3]), simple=True)
      return usage()  
    elif 'orient' in args:
      if len(args) == 4:
        return self.cmd_orientation(float(args[1]),float(args[2]),float(args[3]))
      return usage()    
    elif 'set_home' in args:
      if len(args) == 1:
        return self.set_piksi_home()
      if len(args) == 2:
        return self.set_piksi_home_from_ip(args[1])
      if len(args) == 4:
        return self.set_piksi_home_manually(int(args[1]),int(args[2]),int(args[3]))
      return usage()
    elif 'ok' in args:
      self.okay = True
      self.cleared_to_execute.set()
      return
    elif 'no' in args:
      self.okay = False
      self.cleared_to_execute.set()
      return
    elif 'up' in args:
      if len(args) == 2:
        return self.set_alt_comp(int(args[1]), relative=True)
      return usage()
    elif 'north' in args:
      if len(args) == 2:
        return self.set_pos_comp(int(args[1]), north=True)
      return usage()
    elif 'east' in args:
      if len(args) == 2:
        return self.set_pos_comp(int(args[1]), east=True)
      return usage()
    elif 'set_alt_comp' in args:
      if len(args) == 2:
        return self.set_alt_comp(int(args[1]), relative=False)
      return usage()
    elif 'ahrs_reset_home' in args:
      return self.ahrs_reset_home()


    return usage()

  # ===========================================================================
  # Main Module Runloop
  # ===========================================================================

  def set_alt_comp(self, mm_up, relative=True):
    if relative:
      self.alt_comp_up += mm_up
    else:
      self.alt_comp_up = mm_up

  def set_pos_comp(self, mm, north=False, east=False, relative=False):
    if north:
      if relative:
        self.north_comp += mm
      else:
        self.north_comp = mm
    elif east:
      if relative:
          self.east_comp += mm
      else:
          self.east_comp = mm
    else:
      print "set_pos_comp: direction not specified!"
        

  def set_piksi_home(self):
    '''
    THIS is the main entrypoint function, all the others are helpers.
    '''
    self._update_vehicle_home()

    # NJ Hack SIGGRAPH 2016:
    # return self.set_piksi_home_from_mavlink()

    return self.set_piksi_home_from_solo_sbp()

  def set_piksi_home_from_solo_sbp(self):
    self.set_piksi_home_from_ip('solo_sbp')

  def set_piksi_home_from_mavlink(self):
    print "Setting Piksi home from MAVLINK"

    if self.spooky_vehicle_fake_ned[0] != 0:
      self.vehicle_home_ned = self.spooky_vehicle_fake_ned
      print "Set home to fake NED location of: ", self.spooky_vehicle_fake_ned
    else:
      print "No fake spooky location available."

  def set_piksi_home_from_ip(self, piksi_ip):
    # OK, we grab the current position of a Piksi baseline
    # and save that as the NED vector to translate from 

    systemstate = self.main.modules.get_modules('systemstate')
    
    if not systemstate:
      print "No systemstate loaded, can't grab home."
      return
    systemstate = systemstate[0]

    ned, msg = systemstate.get_piski_baseline_ned(piksi_ip)
    if msg:
      print msg

    if ned:
      self.vehicle_home_ned = ned
    else:
      "Did not update piksi_home"

    print "vehicle_home_ned =", ned

  def set_piksi_home_manually(self, n_mm, e_mm, d_mm):
    self._update_vehicle_home()
    self.vehicle_home_ned = [n_mm, e_mm, d_mm]

  def run(self):
    try:

      CheckIntegrity = spooky.DoPeriodically(self.check_vehicle, 1.0)

      with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as camapi_udp:
        camapi_udp.setblocking(1) 
        camapi_udp.settimeout(0.02) # 50Hz
        camapi_udp.bind((self.bind_ip, self.bind_port))

        print "Module %s listening on %s:%s" % (self, self.bind_ip, self.bind_port)

        self.ready()

        while not self.stopped():

          # Exposing a Camera API
          try:
            camapi_data, camapi_addr = camapi_udp.recvfrom(4096)
            self.handle_camapi(camapi_data, camapi_addr)
          except (socket.error, socket.timeout) as e:
            pass

          CheckIntegrity.tick()


    except SystemExit:
      pass
    except:
      traceback.print_exc()
    finally:
      self.disconnect()


def init(main, instance_name=None):
  module = SoloModule(main, instance_name=instance_name)
  module.start()
  return module
