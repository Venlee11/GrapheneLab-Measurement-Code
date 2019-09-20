#!/usr/bin/python
# -*- coding: utf-8 -*-

"""

As of Feb 2016 this controls the He pot.
The pot thermometer is read by the keithley 2700 at channel 109
The pot calibration file is in D: eoin Thermometers
The pot heater is on AOUT1 of the lakeshore 370

original author : Eoin O'Farrell
current author : Huang Junye
last edited : Apr 2019


	The daemon listens for commands to change the control loop or setpoint
	The daemon broadcasts the current temperature

	!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
	temperature units are Kelvin
	!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

ToDo:

	Listen
	Broadcast
	Initialize
	ReadPico
	CalcPID
	setTCS

	26.05.2017 Alex
	read_all_temperature was updated. all temperatures are shown. Use only for cooling down process.
"""
import asyncore
import logging
import visa as visa
import string as string
import re as res
import time
from scipy import interpolate
from datetime import datetime
import os

import numpy as np
import utils.pid_control as pid_control
import utils.socket_subs as socket_subs
import utils.visa_subs as visa_subs

class TControl:

	"""
	For the 16T there are only 2 temperatures of interest the 1 - the VTI
	and 2 - the probe. All other temperatures are read by a keithley and presently
	controlled by the cryogenic software.
	The lakeshore 370 has two loops, the main (25W) heater output is for the VTI (loop 1) and
	the Analog Output 2 (1W) is used for the probe (loop 2).
	Analog Output 1 is used for the He pot heater that is controlled by the cryogenic software.
	Therefore there are only two temperatures addressed by this program and both are controlled
	by the 370 loops meaning that we only ever need to write the setpoint.
	So...
	"""
	# Initialization call, initialize LS340 visa and start the server
	# server always runs at 18871
	def __init__(self, file_name):
		self.visa = visa_subs.initialize_gpib(12, 0, query_delay="0.04")
		# start the server
		address = ('localhost',18871)
		self.server = socket_subs.SockServer(address)

		t_sensor_name = ["1st stage", "Shield", "2nd stage #1", "2nd Stage #2", "Magnet inner", "Magner outer", "Switch", "Magnet support", "He Pot", "VTI upper HEx"]
		t_sensor_path="d:\eoin\programs\Thermometers\\"
		t_sensor_calibration = ["1)1st Stage.txt", "2)Shield.txt", "3)2nd Stage 1.txt", "4)2nd Stage 2.txt", "5)Magnet Inner.txt", "6)Magnet Outer.txt", "7)Switch.txt", "8)Magnet Support.txt", "9)He Pot.txt", "10)VTI Upper HEx.txt"]
		self.calibration_x = np.zeros((129,11))
		self.calibration_y = np.zeros((129,11))
		for i in range(1, 10):
			calibration_path = t_sensor_path+t_sensor_calibration[i-1]
			calibration_xy = np.genfromtxt(calibration_path,skip_header=1)
			self.calibration_x[:,i-1] = np.flipud(calibration_xy[:,1])
			self.calibration_y[:,i-1] = np.flipud(calibration_xy[:,0])

		self.pot_visa = visa_subs.initialize_gpib(6, 0, query_delay="0.04")
		self.pot_visa.write("FORM:ELEM READ")  #Configure the 2700 to only return resistance

		self.temperature = np.zeros((2,))
		self.pot_temperature = 0.0
		self.sensor_name = ["VTI", "Probe"]
		self.sensor_location = [0,1]
		# there are 2 set temps
		self.set_temp = np.zeros((2,))
		self.status = -1
		# but there are only 2 loops :(
		self.loop_number = [1,2]
		self.zone_control = [False, False]
		self.loop_enable = [False, False]
		self.pid_vals = np.empty((3,3))

		# For 9T there are 3 heater outputs main heater = VTI, Analog 1 = 4He Pot
		# Analog 2 = Probe
		# so we have the commands to read the heaters
		self.heater_command = ["HTR?", "AOUT?2"]
		self.heater_current = np.zeros((len(self.loop_number),))
		self.delta_temp = np.zeros((2,))
		self.max_set_temp = np.array([300.0,310.0])

		# The acceptable error in temperature as a factor of the
		# set temperature e.g. 2.0 K x 0.005 = 0.01 K
		self.error_temp = 0.001
		# The acceptable stability as a factor of the error
		self.error_delta_temp = 0.05
		# We maintain the VTI slightly below the probe
		self.delta_probe = 5.0

		# Sweep description, temperature denotes probe temperatures
		self.sweep_finish = 0.0
		self.sweep_rate = 0. # rate in K/min
		self.sweep_time = 0. # in seconds
		self.sweep_direction = 1.0
		self.sweep_start_time = []
		self.sweep_time_length = 0.0
		self.sweep_max_over_time = 15.0 # minutes

		# Status Parameters
		# Modes are 0: set, 1: sweep
		self.at_set = False
		self.sweep_mode = False
		self.status_msg = 0

		# Status events, every so often we push a status to the terminal
		self.status_interval = 0.25 # minutes
		self.last_status_time = datetime.now()

		# Turn off ramp mode
		self.visa.write("RAMP 1,0,0")
		self.visa.write("RAMP 2,0,0")

		#set date interval to creat noew log file, default = 1 day(s)
		self.date_interval = 1
		self.file_name = file_name

		return

	# The ls340 often formats replies X,Y,Z -> return selection of values
	def ls_340_ask_multi(self,Query,Return):
		reply = self.visa.ask(Query)
		reply = reply.split(",")
		answer = list()
		for i in Return:
			answer.append(reply[i])

		return answer

	# Get the loop paramters: enabled, control mode, PID, set_points
	def get_loop_params(self):

		for i,v in enumerate(self.loop_number):
			# enabled?
			reply = self.ls_340_ask_multi(" ".join(("CSET?","%d" % v)),[2])
			self.loop_enable[i] = bool(int(reply[0]))
			#print "%s" % reply
			# Control mode
			reply = self.visa.ask(" ".join(("CMODE?","%d" % v)))
			if reply == "2":
				self.zone_control[i] = True
			else:
				self.zone_control[i] = False
			#print "%s" % reply
			# PIDs
			reply = self.ls_340_ask_multi(" ".join(("PID?","%d" % v)),[0,1,2])
			#print "%s" % reply
			for j,u in enumerate(reply):
				self.pid_vals[j,i] = float(u)
			# set_points
			reply = self.visa.ask(" ".join(("SETP?","%d" % (i+1))))
			#print "%s" % reply
			self.set_temp[i] = float(reply)

		return

	def read_pot_temperature(self):
		#reply = self.pot_visa.ask(":FETC?")
		#reply = reply.split(",")
		#res = float(reply[0])
		#self.pot_temperature = self.he_pot_fn(res)
		self.pot_temperature = 0.0
		return res

	def read_all_temperature(self):
		t_sensor_name = ["1st stage", "Shield", "2nd stage #1", "2nd Stage #2", "Magnet inner", "Magner outer", "Switch", "Magnet support", "He Pot", "VTI upper HEx"]
		t_sensor_path="d:\eoin\programs\Thermometers\\"
		t_sensor_calibration = ["1)1st Stage.txt", "2)Shield.txt", "3)2nd Stage 1.txt", "4)2nd Stage 2.txt", "5)Magnet Inner.txt", "6)Magnet Outer.txt", "7)Switch.txt", "8)Magnet Support.txt", "9)He Pot.txt", "10)VTI Upper HEx.txt"]
		#t_sensor_calibration = ["1)1st Stage.txt", "2)Shield.txt", "3)2nd Stage 1.txt", "4)2nd Stage 2.txt", "5)Magnet Inner.txt", "6)Magnet Outer.txt", "7)Switch.txt", "8)Magnet Support.txt", "9)He Pot.txt", "10)VTI Upper HEx.txt"]
		temp_string = ''
		for i in range(1, 10):
			self.pot_visa.write("INIT:CONT OFF")
			Ch = 100 + i
			message=" ".join(("ROUT:CLOS (@","%d" % Ch))+")"
			self.pot_visa.write(message)
			#self.pot_visa.write("INIT")
			reply = self.pot_visa.ask("READ?")
			#print "%s" % reply
			reply = reply.split(",")
			res = list(map(float, reply))

			self.he_pot_fn = interpolate.interp1d(self.calibration_x[:,i-1], self.calibration_y[:,i-1])
			res=self.he_pot_fn(res)
			print("%s = %f K" % (t_sensor_name[i-1], res))
			temp_string += "%.3f," % res
			#self.pot_temperature = res[8]
			if Ch==109:
				self.pot_temperature = res

		date_now = datetime.now()
		delta_date = date_now - date_begin
		if delta_date.days > control.date_interval:
			date = time.strftime("%Y%m%d", time.localtime())
			self.file_name = 'temperature_'+date+'.log'
		else:
			pass

		file_exist = os.path.exists(self.file_name)
		if file_exist:
			logging.warning(temp_string)
		else:
			f = open(self.file_name,'a')
			header = 'Date, 1st Stage, Shield, 2nd Stage #1, 2nd Stage #2, Magnet inner, Magnet outter, Switch, Magnet support, He Pot'
			f.write(header)
			f.write('\n')
			f.close()

			logging.basicConfig(filename=self.file_name, filemode='a', format='%(asctime)s,%(message)s', level=logging.WARNING)
			logging.warning(temp_string)
		return

	def read_temp_heater(self):
		# read the temperature and heater outputs
		old_temp = self.temperature
		for i,v in enumerate(self.sensor_location):
			# temperature
			reply = self.visa.ask(" ".join(("KRDG?","%d" % v)))
			self.temperature[i] = float(reply)
		self.delta_temp = self.temperature - old_temp

		# read the heaters
		for i,v in enumerate(self.heater_command):
			reply = self.visa.ask(v)
			self.heater_current[i] = float(reply)

		return

	# write set points to the instrument for the probe
	def update_set_temp(self,set_temp):

		set_temp = [set_temp - self.delta_probe, set_temp]
		for i,v in enumerate(set_temp):
			if (v < self.max_set_temp[i]) and (v >= 0.0):
				self.set_temp[i] = v
			elif (v < 0.0):
				self.set_temp[i] = 0.0
			else:
				self.set_temp[i] = self.max_set_temp[i]

		return

	def write_set_point(self):

		for i,v in enumerate(self.loop_number):
			self.visa.write("".join(("SETP ","%d," % v, "%.3f" % self.set_temp[i])))

		return

	# Update the parameter at_set for the probe
	def update_at_set(self):
		set = False
		# The stability measure is v crude
		stable = False
		# 1 = Sweep
		error_factor = abs(self.temperature[1] - self.set_temp[1])/self.temperature[1]
		delta_temp_factor = abs(self.delta_temp[1])/self.temperature[1]

		if error_factor < self.error_temp:
			set = True
		if delta_temp_factor < self.error_delta_temp:
			stable = True
		self.at_set = set and stable
		return

	# Interpret a message from the socket, current possible messages are
	# SET ...  -  set probe the temperature
	# SWP ...  -  sweep the probe temperature
	def read_msg(self,msg):
		msg = msg.split(" ")

		if msg[0] == "SET":
			try:
				new_set = float(msg[1])
				# Only interpret new set_points if the change is >50mK
				if abs(self.set_temp[1]-new_set) > 0.05:
					if self.sweep_mode:
						# We are sweeping so kill the sweep
						print("Killing sweep...")
						self.visa.write("RAMP 1,0,0")
						self.visa.write("RAMP 2,0,0")
					self.update_set_temp(new_set)
					# Set at set to be false and write the new set point
					self.at_set = False
					self.sweep_mode = False
					self.write_set_point()
					print("Got probe set point from socket %.2f\n" % self.set_temp[1])
			except:
				pass

		if msg[0] == "SWP":
			try:
				self.sweep_finish = float(msg[1])
				if abs(self.sweep_finish - self.set_temp[1]) > 0.05:
					self.sweep_rate = abs(float(msg[2]))
					self.sweep_max_over_time = abs(float(msg[3]))
					# Check if the sweep is up or down
					if self.sweep_finish >= self.set_temp[1]:
						self.sweep_direction = 1.0
					else:
						self.sweep_direction = -1.0
					# Put the LS340 into ramp mode
					self.visa.write("RAMP 1,1,%.3f" % self.sweep_rate)
					self.visa.write("RAMP 2,1,%.3f" % self.sweep_rate)
					self.at_set = False
					self.sweep_time_length = abs(self.set_temp[1] - self.sweep_finish)/self.sweep_rate
					print("Got temperature sweep to %.2f K at %.2f K/min... Sweep takes %.2f minutes, maximum over time is %.2f"
						% (self.sweep_finish, self.sweep_rate, self.sweep_time_length, self.sweep_max_over_time))
					# Write the finish temp
					self.update_set_temp(self.sweep_finish)
					# Write the set_point to start the ramp
					self.write_set_point()
					self.sweep_mode = True
					self.sweep_start_time = datetime.now()
					print("Starting the sweep\n")
			except:
				pass

		if msg[0] == "T_ERROR":
			try:
				self.error_temp = float(msg[1])
			except:
				pass

		if msg[0] == "DT_ERROR":
			try:
				self.error_delta_temp = float(msg[1])
			except:
				pass

		return


	def sweep_control(self):

		# We are sweeping so check if the sweep is finished
		dt = datetime.now() - self.sweep_start_time
		dt = dt.seconds/60.0

		if dt > (self.sweep_time_length + self.sweep_max_over_time):
			# The sweep ran out of time, stop it
			sweep_finished = True
			print("Sweep over time... Finishing...")
		elif (self.temperature[1] - self.sweep_finish)*self.sweep_direction > 0.0:
			sweep_finished = True
			print("Final temperature reached... Finishing...")
		else:
			sweep_finished = False

		if sweep_finished:
			# The sweep is finished stop ramping and change the mode
			self.visa.write("RAMP 1,0,0")
			self.visa.write("RAMP 2,0,0")
			# Write the set_point to the current temperature
			self.update_set_temp(self.temperature[1])
			self.write_set_point()
			self.sweep_mode = False

		return



	def update_status_msg(self):
		# TDaemon status messages:
		# 0 = Not ready
		# 1 = Ready

		if self.at_set and not self.sweep_mode:
			status = 1 # Ready
		else:
			status = 0 # Not ready

		self.status_msg = status
		return

	def print_status(self):
		status_string = ""
		for i,v in enumerate(self.temperature):
			status_string += "%s = %.3f K; " % (self.sensor_name[i],self.temperature[i])

		status_string += "He Pot Temp = %.2f K;" % self.pot_temperature
		status_string += "status message = %d\n" % self.status_msg
		print(status_string)
		self.last_status_time = datetime.now()
		return


if __name__ == '__main__':

	date_begin = datetime.now()
	date = time.strftime("%Y%m%d", time.localtime())
	file_name = 'temperature_'+date+'.log'
	# Initialize a PID controller for the 4He Pot
	pid = pid_control.PID(p=500,i=10.,d=0,derivator=0,integrator=0,integrator_max=250,integrator_min=-50)

	control = TControl(file_name)

	control.get_loop_params()
	control.read_temp_heater()
	#control.read_pot_temperature()
	control.read_all_temperature()
	control.print_status()
	#control.set_temp[2] = 3.7 # set temperature for the He Pot
	pid.set_point = control.set_temp[1]

	while 1:

		# Read the picowatt and calculate the temperature
		control.read_temp_heater()
		#control.read_pot_temperature()
		control.read_all_temperature()
		control.update_at_set()
		control.update_status_msg()

		#control.get_loop_params()

		# Push the readings to clients and read messages
		for j in control.server.handlers:
			j.to_send = ",%.4f %.4f %.4f %.4f %d" % (control.temperature[0], control.temperature[1], control.heater_current[0], control.heater_current[1], control.status_msg)
			socket_msg = j.received_data
			if socket_msg:
				control.read_msg(socket_msg)
		asyncore.loop(count=1,timeout=0.001)


		# if we are sweeping we do some things specific to the sweep
		if control.sweep_mode:
			control.sweep_control()

		# check if we should send an update
		update_time = datetime.now() - control.last_status_time
		if update_time.seconds/60.0 >= control.status_interval:
			control.print_status()

		#Potres = Potresistance(pot_visa)
		#PotT = he_pot_fn(Potres)
		# print PotT, Potres
		# Now we do some PID stuff in software to control the He Pot
		new_pid = pid.update(control.pot_temperature)
		#print "%.3f, %.3f" % (control.temperature[2],new_pid)
		if new_pid > 100.0:
			new_pid = 100.0
		elif new_pid < 0.0:
			new_pid = 0.0
		control.visa.write("".join(("ANALOG 1,0,2,,,,,%.2f" % new_pid)))

		time.sleep(10)

	control.visa.close()
