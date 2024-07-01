import telebot
import logging
import time
import json
from datetime import datetime, timedelta
from typing import Optional

LONG_LONG_TIME = datetime(2100, 1, 1)
ID_REMIND_DURATION = timedelta(days=2)
BAN_NOTSENT_WARNING = timedelta(minutes=10)
ALL_CONTENT_TYPES = ('animation', 'audio', 'contact', 'dice', 'document',
	'game', 'location', 'photo', 'sticker', 'story', 'text', 'venue', 'video',
	'video_note', 'voice')

TMessage = telebot.types.Message

bot: telebot.TeleBot = None
db = None

bot_self_id: int = None
target_group: Optional[int] = None
welcome_text: str = None
reply_text: str = None

def init(config: dict, _db):
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
	assert not bot.threaded
	while True:
		try:
			bot.polling(non_stop=True, long_polling_timeout=60)
		except Exception as e:
			# you're not supposed to call .polling() more than once but I'm left with no choice
			logging.warning("%s while polling Telegram, retrying.", type(e).__name__)
			time.sleep(1)

def callwrapper(f) -> Optional[str]:
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
	def __enter__(self) -> 'User':
		return self.obj
	def __exit__(self, exc_type, *_):
		if exc_type is None:
			db[self.key] = self.obj

class User():
	id: int
	username: str
	realname: str
	last_messaged: datetime
	banned_until: Optional[datetime]
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
#

def db_get_user(id) -> User:
	return db["u%d" % id]

def db_modify_user(id, allow_new=False) -> ModificationContext:
	key = "u%d" % id
	obj = db.get(key)
	if obj is None:
		if allow_new:
			obj = User()
		else:
			raise KeyError
	return ModificationContext(key, obj)

### Main stuff

def handle_msg(ev: TMessage):
	db_auto_sync()
	if ev.chat.type in ("group", "supergroup"):
		if ev.chat.id == target_group:
			return handle_group(ev)
		logging.warning("Got message from group %d which "
			"we're not supposed to be in", ev.chat.id)
	elif ev.chat.type == "private":
		return handle_private(ev)

def handle_group(ev: TMessage):
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

	user = db_get_user(user_id)
	now = datetime.now()
	if user.banned_until is not None and (user.banned_until >= now and
		now - user.last_messaged >= BAN_NOTSENT_WARNING):
		msg = "Message was not delivered, unban recipient first."
		return callwrapper(lambda: bot.send_message(target_group, msg))

	# deliver message
	res = callwrapper(lambda: bot.copy_message(user_id, ev.chat.id, ev.message_id))
	if res == "blocked":
		callwrapper(lambda: bot.send_message(target_group, "Bot was blocked by user."))

def handle_group_command(ev: TMessage, user_id: int, c: str, arg: str):
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

def handle_private(ev: TMessage):
	if target_group is None:
		logging.error("Target group not set, dropping message from user!")
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
		c = ev.text[1:].split(" ", 1)[0]
		if handle_private_command(ev, user, c):
			return

	# deliver message
	if ev.forward_origin is not None:
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

	with db_modify_user(user.id) as user:
		user.last_messaged = now

def handle_private_command(ev: TMessage, user, c):
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
