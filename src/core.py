import telebot
import logging
import time
import json
from datetime import datetime, timedelta

LONG_LONG_TIME = datetime(2100, 1, 1)
ID_REMIND_DURATION = timedelta(days=2)
ALL_CONTENT_TYPES = ["text", "location", "venue", "contact", "animation",
	"audio", "document", "photo", "sticker", "video", "video_note", "voice"]

bot = None
db = None

bot_self_id = None
target_group = None
welcome_text = None
reply_text = None

def init(config, _db):
	global bot, db, bot_self_id, target_group, welcome_text, reply_text
	if not config.get("bot_token"):
		logging.error("No telegram token specified.")
		exit(1)

	logging.getLogger("urllib3").setLevel(logging.WARNING) # very noisy with debug otherwise
	bot = telebot.TeleBot(config["bot_token"], threaded=False)
	db = _db
	if config.get("target_group"):
		target_group = int(config["target_group"])
	welcome_text = config["welcome_text"]
	reply_text = config["reply_text"]

	set_handler(handle_msg, content_types=ALL_CONTENT_TYPES)
	bot_self_id = bot.get_me().id
	logging.info("Startup OK")

def set_handler(func, *args, **kwargs):
	def wrapper(*args, **kwargs):
		try:
			func(*args, **kwargs)
		except Exception as e:
			logging.exception("Exception raised in event handler")
	bot.message_handler(*args, **kwargs)(wrapper)

def run():
	while True:
		try:
			bot.polling(none_stop=True)
		except Exception as e:
			# you're not supposed to call .polling() more than once but I'm left with no choice
			logging.warning("%s while polling Telegram, retrying.", type(e).__name__)
			time.sleep(1)

def callwrapper(f):
	while True:
		try:
			f()
		except telebot.apihelper.ApiException as e:
			status = check_telegram_exc(e)
			if not status:
				continue
			return status
		return

def check_telegram_exc(e):
	errmsgs = ["bot was blocked by the user", "user is deactivated",
		"PEER_ID_INVALID", "bot can't initiate conversation"]
	if any(msg in e.result.text for msg in errmsgs):
		return "blocked"

	if "Too Many Requests" in e.result.text:
		d = json.loads(e.result.text)["parameters"]["retry_after"]
		d = min(d, 30) # supposedly this is in seconds, but you sometimes get 100 or even 2000
		logging.warning("API rate limit hit, waiting for %ds", d)
		time.sleep(d)
		return False # retry

	logging.exception("API exception")
	return "exception"

### db

class ModificationContext():
	def __init__(self, key, obj):
		self.key = key
		self.obj = obj
	def __enter__(self):
		return self.obj
	def __exit__(self, exc_type, *_):
		if exc_type is None:
			db[self.key] = self.obj

class User():
	def __init__(self):
		self.id = None
		self.username = None
		self.realname = None
		self.last_messaged = None
		self.banned_until = None
	def __eq__(self, other):
		if isinstance(other, User):
			return self.id == other.id
		return NotImplemented
	def __str__(self):
		return "<User id=%d>" % self.id
	def defaults(self):
		self.last_messaged = datetime(1970, 1, 1)

# this is kinda shit
db_last_sync = 0
def db_auto_sync():
	global db_last_sync
	now = int(time.time())
	if now > db_last_sync + 15:
		db_last_sync = now
		db.sync()

def db_get_user(id):
	return db["u%d" % id]

def db_modify_user(id, allow_new=False):
	key = "u%d" % id
	obj = db.get(key)
	if obj is None:
		if allow_new:
			obj = User()
		else:
			raise KeyError
	return ModificationContext(key, obj)

### Main stuff

def handle_msg(ev):
	db_auto_sync()
	if ev.chat.type in ("group", "supergroup"):
		if ev.chat.id == target_group:
			return handle_group(ev)
		logging.warning("Got message from group %d which "
			"we're not supposed to be in", ev.chat.id)
	elif ev.chat.type == "private":
		return handle_private(ev)

def handle_group(ev):
	if ev.reply_to_message is None:
		return
	if ev.reply_to_message.from_user.id != bot_self_id:
		return

	user_id = db.get("m%d" % ev.reply_to_message.message_id)
	logging.debug("found id = %d mapped to user %s", ev.reply_to_message.message_id, user_id)
	if user_id is None:
		logging.warning("Couldn't find replied to message in target group")
		return

	# handle commands
	if ev.content_type == "text" and ev.text.startswith("/"):
		c, _, arg = ev.text[1:].partition(" ")
		return handle_group_command(ev, user_id, c, arg)

	# deliver message
	res = callwrapper(lambda: resend_message(user_id, ev))
	if res == "blocked":
		callwrapper(lambda: bot.send_message(target_group, "Bot was blocked by user."))

def handle_group_command(ev, user_id, c, arg):
	if c == "info":
		msg = format_user_info(db_get_user(user_id))
		return callwrapper(lambda: bot.send_message(target_group, msg, parse_mode="HTML"))
	elif c == "ban":
		delta = parse_timedelta(arg)
		if not delta:
			until = LONG_LONG_TIME
			msg = "User banned permanently."
		else:
			until = datetime.now() + delta
			msg = "User banned until %s." % format_datetime(until)
		with db_modify_user(user_id) as user:
			user.banned_until = until
		return callwrapper(lambda: bot.send_message(target_group, msg))
	elif c == "unban":
		msg = None
		with db_modify_user(user_id) as user:
			if user.banned_until is None or user.banned_until < datetime.now():
				msg = "User was not banned or ban expired already."
			else:
				user.banned_until = None
				msg = "User was unbanned."
			return callwrapper(lambda: bot.send_message(target_group, msg))

def handle_private(ev):
	if target_group is None:
		logging.error("Target group not set, dropping message from user")
		return

	# refresh user in db
	now = datetime.now()
	with db_modify_user(ev.chat.id, allow_new=True) as user:
		if user.id is None:
			user.defaults()
			user.id = ev.chat.id
			assert ev.chat.id == ev.from_user.id
		user.username = ev.from_user.username
		user.realname = ev.from_user.first_name
		if ev.from_user.last_name:
			user.realname += " " + ev.from_user.last_name

	# check things
	error = None
	if user.banned_until is not None:
		if now >= user.banned_until:
			pass
		elif user.banned_until >= LONG_LONG_TIME:
			error = "You cannot message the support bot."
		else:
			error = "You cannot message the support bot now, try again later."
	if error is not None:
		return callwrapper(lambda: bot.send_message(ev.chat.id, error))

	# handle commands
	if ev.content_type == "text" and ev.text.startswith("/"):
		c = ev.text[1:].split(" ", 2)[0]
		if handle_private_command(ev, user, c):
			return

	# deliver message
	if (ev.forward_from is not None or ev.forward_from_chat is not None
		or ev.json.get("forward_sender_name") is not None):
		msg = "It is not possible to forward messages here."
		return callwrapper(lambda: bot.send_message(ev.chat.id, msg))

	if now - user.last_messaged >= ID_REMIND_DURATION:
		msg = "---------------------------------------\n"
		msg += format_user_info(user)
		callwrapper(lambda: bot.send_message(target_group, msg, parse_mode="HTML"))
	def f(user_id=user.id):
		ev2 = bot.forward_message(target_group, ev.chat.id, ev.message_id)
		db["m%d" % ev2.message_id] = user_id
		logging.debug("delivered msg from %s -> id = %d", user, ev2.message_id)
	callwrapper(f)

	if reply_text:
		callwrapper(lambda: bot.send_message(ev.chat.id, reply_text, parse_mode="HTML"))

	# this sucks too
	with db_modify_user(user.id) as user:
		user.last_messaged = now

def handle_private_command(ev, user, c):
	if c == "start":
		callwrapper(lambda: bot.send_message(ev.chat.id, welcome_text, parse_mode="HTML"))
		return True
	elif c == "stop":
		return True

### Helpers

def str_is_printable(s):
	NOT_PRINTABLE = (0x20, 0x115f, 0x1160, 0x3164, 0xffa0)
	return any((c.isprintable() and ord(c) not in NOT_PRINTABLE) for c in s)

def parse_timedelta(s):
	if len(s) < 2 or not s[:-1].isdigit():
		return
	suff = s[-1].lower()
	suff = ({"s": 1, "m": 60, "h": 60*60, "d": 24*60*60, "w": 7*24*60*60}).get(suff)
	if not suff:
		return
	return timedelta(seconds=int(s[:-1]) * suff)

def escape_html(s):
	ret = ""
	for c in s:
		if c in ("<", ">", "&"):
			c = "&#" + str(ord(c)) + ";"
		ret += c
	return ret

def format_datetime(dt):
	return dt.strftime("%Y-%m-%d %H:%M:%S")

def format_user_info(user):
	realname = user.realname
	if not str_is_printable(realname):
		realname = "<empty name>"
	s = "User: <a href=\"tg://user?id=%d\">%s</a>" % (
		user.id, escape_html(realname))
	if user.username is not None:
		s += " (@%s)" % escape_html(user.username)
	s += "\nID: <code>%d</code>" % user.id
	return s

def resend_message(chat_id, ev):
	# re-send message based on content type
	if ev.content_type == "text":
		return bot.send_message(chat_id, ev.text)
	elif ev.content_type == "photo":
		photo = sorted(ev.photo, key=lambda e: e.width*e.height, reverse=True)[0]
		return bot.send_photo(chat_id, photo.file_id, caption=ev.caption)
	elif ev.content_type == "audio":
		kwargs = {
			"caption": ev.caption,
			"performer": ev.audio.performer,
			"title": ev.audio.performer,
		}
		return bot.send_audio(chat_id, ev.audio.file_id, **kwargs)
	elif ev.content_type == "animation":
		return bot.send_animation(chat_id, ev.animation.file_id, caption=ev.caption)
	elif ev.content_type == "document":
		return bot.send_document(chat_id, ev.document.file_id, caption=ev.caption)
	elif ev.content_type == "video":
		return bot.send_video(chat_id, ev.video.file_id, caption=ev.caption)
	elif ev.content_type == "voice":
		return bot.send_voice(chat_id, ev.voice.file_id, caption=ev.caption)
	elif ev.content_type == "video_note":
		return bot.send_video_note(chat_id, ev.video_note.file_id)
	elif ev.content_type == "location":
		kwargs = {
			"latitude": ev.location.latitude,
			"longitude": ev.location.longitude,
		}
		return bot.send_location(chat_id, **kwargs)
	elif ev.content_type == "venue":
		kwargs = {
			"latitude": ev.venue.location.latitude,
			"longitude": ev.venue.location.longitude,
		}
		for prop in ("title", "address", "foursquare_id", "foursquare_type", "google_place_id", "google_place_type"):
			kwargs[prop] = getattr(ev.venue, prop)
		return bot.send_venue(chat_id, **kwargs)
	elif ev.content_type == "contact":
		kwargs = {}
		for prop in ("phone_number", "first_name", "last_name"):
			kwargs[prop] = getattr(ev.contact, prop)
		return bot.send_contact(chat_id, **kwargs)
	elif ev.content_type == "sticker":
		return bot.send_sticker(chat_id, ev.sticker.file_id)
	else:
		raise NotImplementedError("content_type = %s" % ev.content_type)
