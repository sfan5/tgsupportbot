#!/usr/bin/env python3
import logging
import yaml
import threading
import sys
import os
import shelve
import getopt
from pickle import Unpickler

from . import bot

def start_new_thread(func, join=False, args=(), kwargs=None):
	t = threading.Thread(target=func, args=args, kwargs=kwargs)
	if not join:
		t.daemon = True
	t.start()
	if join:
		t.join()

def usage():
	print("Usage: %s [-q|-d] [-c file]" % sys.argv[0])
	print("Options:")
	print("  -h    Display this text")
	print("  -q    Quiet, set log level to WARNING")
	print("  -c    Location of config file (default: ./config.yaml)")

# well this is just dumb
class RenamingUnpickler(Unpickler):
	def find_class(self, module, name):
		if module == "src.core":
			module = "supportbot.bot"
		return super().find_class(module, name)

def main(configpath, loglevel=logging.INFO):
	with open(configpath, "r") as f:
		config = yaml.safe_load(f)

	logging.basicConfig(format="%(levelname)-7s [%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S", level=loglevel)

	shelve.Unpickler = RenamingUnpickler
	db = shelve.open(config["database"])

	bot.init(config, db)

	try:
		start_new_thread(bot.run, join=True)
	except KeyboardInterrupt:
		logging.info("Interrupted, exiting")
		db.close()
		os._exit(1)

if __name__ == "__main__":
	try:
		opts, args = getopt.getopt(sys.argv[1:], "hqc:", ["help"])
	except getopt.GetoptError as e:
		print(str(e))
		exit(1)

	# Process command line args
	def readopt(name):
		for e in opts:
			if e[0] == name:
				return e[1]
	if readopt("-h") is not None or readopt("--help") is not None:
		usage()
		exit(0)
	loglevel = logging.INFO
	if readopt("-q") is not None:
		loglevel = logging.WARNING
	configpath = "./config.yaml"
	if readopt("-c") is not None:
		configpath = readopt("-c")

	# Run the actual program
	main(configpath, loglevel)
