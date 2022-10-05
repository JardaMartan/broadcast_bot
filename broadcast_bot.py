import asyncio
import aiohttp
import os
import io
import sys
import signal
import json
import codecs
import re
import base64
import _thread
import time
import concurrent.futures
import logging
from dotenv import load_dotenv, find_dotenv

dotenv_file = os.getenv("DOT_ENV_FILE")
if dotenv_file:
    load_dotenv(find_dotenv(dotenv_file))
else:
    load_dotenv(find_dotenv())
    
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  [%(levelname)7s]  [%(module)s.%(name)s.%(funcName)s]:%(lineno)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

loop = asyncio.get_event_loop()

# see documentation at https://webexteamssdk.readthedocs.io/en/latest/user/api.html
from webexteamssdk import WebexTeamsAPI, ApiError, AccessToken
webex_api = WebexTeamsAPI()

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder
from urllib.parse import urlparse
import urllib3
from flask import Flask, request, redirect, url_for, make_response

DEFAULT_AVATAR_URL= "http://bit.ly/SparkBot-512x512"
PORT=5050
MAX_URL_RETRIES = 10
DEFAULT_CONFIG = json.loads("""
{
  "source": {
    "bots_own_org": true,
    "from_sender_list": false,
    "sender_list": {}
  },
  "destination": {
    "bots_own_org": false,
    "senders_own_org": true
  },
  "membership": {
    "bots_own_org": false
  }
}
""")

EMPTY_CARD = {
    "contentType": "application/vnd.microsoft.card.adaptive",
    "content": None,
}

flask_app = Flask(__name__)
flask_app.config["DEBUG"] = True
requests.packages.urllib3.disable_warnings()

@flask_app.before_first_request
def before_first_request():
    me = get_bot_info()
    email = me.emails[0]

    if ("@sparkbot.io" not in email) and ("@webex.bot" not in email):
        logger.error("""
You have provided access token which does not belong to a bot ({}).
Please review it and make sure it belongs to your bot account.
Do not worry if you have lost the access token.
You can always go to https://developer.ciscospark.com/apps.html 
URL and generate a new access token.""".format(email))

def get_bot_id():
    bot_id = os.getenv("BOT_ID", None)
    if bot_id is None:
        me = get_bot_info()
        bot_id = me.id
        
    # logger.debug("Bot id: {}".format(bot_id))
    return bot_id
    
def get_bot_info():
    try:
        me = webex_api.people.me()
        if me.avatar is None:
            me.avatar = DEFAULT_AVATAR_URL
            
        # logger.debug("Bot info: {}".format(me))
        
        return me
    except ApiError as e:
        logger.error("Get bot info error, code: {}, {}".format(e.status_code, e.message))
        
def get_bot_name():
    me = get_bot_info()
    return me.displayName
    
@flask_app.before_request
def before_request():
    pass

"""
Startup procedure used to initiate @flask_app.before_first_request
"""
@flask_app.route("/startup")
def startup():
    return "Hello World!"
    
@flask_app.route("/")
def root():
    return "Hello World!"

async def get_room_membership(room_type = ["direct", "group"]):
    membership_list = webex_api.memberships.list()
    for membership in membership_list:
        if membership.json_data.get("roomType") in room_type:
            yield membership.roomId

"""
Handle Webex webhook events.
"""
@flask_app.route("/webhook", methods=["POST"])
async def webex_webhook():
    webhook = request.get_json(silent=True)
    logger.debug("Webhook received: {}".format(webhook))
    res = await handle_webhook_event(webhook)
    logger.debug(f"Webhook hadling result: {res}")

    logger.debug("Webhook handling done.")
    return "OK"
        
@flask_app.route("/webhook", methods=["GET"])
def webex_webhook_preparation():
    bot_info = get_bot_info()
    message = "<center><img src=\"{0}\" alt=\"{1}\" style=\"width:256; height:256;\"</center>" \
              "<center><h2><b>Congratulations! Your <i style=\"color:#ff8000;\">{1}</i> bot is up and running.</b></h2></center>".format(bot_info.avatar, bot_info.displayName)
              
    message += "<center><b>I'm hosted at: <a href=\"{0}\">{0}</a></center>".format(request.url)
    res = loop.run_until_complete(manage_webhooks(request.url))
    if res is True:
        message += "<center><b>New webhook created sucessfully</center>"
    else:
        message += "<center><b>Tried to create a new webhook but failed, see application log for details.</center>"

    return message
        
# @task
async def handle_webhook_event(webhook):
    action_list = []
    
    if webhook.get("resource") == "messages" and webhook.get("event") == "created":
        logger.debug(f"message received")
        bot_info = get_bot_info()
        bot_email = bot_info.emails[0]
        bot_name = bot_info.displayName
        if webhook["data"].get("personEmail") != bot_email:
            try:
                message = webex_api.messages.get(webhook["data"].get("id"))
                sender_info = webex_api.people.get(webhook["data"].get("personId"))
                logger.debug(f"Replicating received message: {message}\nfrom: {sender_info}")
                
                config = load_config()
                if not check_sender(sender_info, bot_info, config):
                    logger.debug(f"sender check failed, broadcast not allowed")
                    return
                
                if message.html is not None:
                    msg_markdown = re.sub(r"<spark-mention.*\/spark-mention>[\s]*", "", message.html)
                else:
                    msg_markdown = message.text if message.text is not None else ""
                group_msg = {"markdown": f"Message from <@personId:{sender_info.id}>:  \n\n{msg_markdown}", "files": message.files}
                direct_msg = {"markdown": f"Message from {sender_info.displayName} ({sender_info.emails[0]}):  \n\n{msg_markdown}", "files": message.files}
                with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                    local_loop = asyncio.get_event_loop()
                    task_list = []
                    async for room_id in get_room_membership(room_type = ["group"]):
                        if check_destination(room_id, sender_info, bot_info, config):
                            task_list.append(local_loop.run_in_executor(executor, create_message, room_id, group_msg))
                    async for room_id in get_room_membership(room_type = ["direct"]):
                        if check_destination(room_id, sender_info, bot_info, config):
                            task_list.append(local_loop.run_in_executor(executor, create_message, room_id, direct_msg))
                    
                    for msg_result in await asyncio.gather(*task_list):
                        logger.info(f"messsage create result: {msg_result}")
            except ApiError as e:
                logger.error(f"Get message failed: {e}.")
                
    elif webhook.get("resource") == "memberships":
        actor_info = webex_api.people.get(webhook["actorId"])
        logger.info(f"my membership {webhook.get('event')} by {actor_info.displayName} ({actor_info.emails[0]}) in space {webhook['data']['roomId']}")
        if webhook.get('event') == "created":
            room_info = webex_api.rooms.get(webhook["data"]["roomId"])
            logger.debug(f"room info: {room_info}")
            bot_info = get_bot_info()
            config = load_config()
            if check_membership(room_info, bot_info, config):
                if room_info.isAnnouncementOnly:
                    logger.debug(f"room is announcement_only, ask actor to make me a moderator")
                    room_decoded = base64.b64decode(room_info.id)
                    room_uuid = re.findall(r"ciscospark:.*\/([^/]+)", room_decoded.decode())[0]
                    room_url = f"webexteams://im?space={room_uuid}"
                    logger.debug(f"room UUID: {room_uuid}, URL: {room_url}")
                    ask_message = f"Space [{room_info.title}]({room_url}) is **Announcement only**, please make sure I am a moderator"
                    try:
                        result = webex_api.messages.create(toPersonId = webhook["actorId"], markdown = ask_message)
                        logger.debug(f"asked actor for moderation: {result}")
                    except ApiError as e:
                        logger.error(f"failed to send message to {actor_info.emails[0]}: {e}")
            else:
                org_info = webex_api.organizations.get(bot_info.orgId)
                logger.debug(f"my org info: {org_info}")
                try:
                    msg_markdown = f"I am allowed to communicate only in Spaces owned by **{org_info.displayName}**."
                    webex_api.messages.create(roomId = room_info.id, markdown = msg_markdown)
                    result = webex_api.memberships.delete(webhook["data"]["id"])
                    logger.debug(f"membership delete result: {result}")
                except ApiError as e:
                    logger.error(f"Webex API error while trying to delete myself from a space: {e}")
            
def create_message(room_id, kwargs):
    try:
        logger.debug(f"received args: {kwargs}")

        msg_data = kwargs.copy()
        try:
            files = msg_data.pop("files", None)
        except KeyError as e:
            logger.debug(f"files not found in {kwargs}")

        result = None
        http = urllib3.PoolManager()
        if files is not None:
            file_headers = {"Authorization": f"Bearer {webex_api.access_token}"}
            logger.debug(f"headers: {file_headers}")
            
            responses = []
            for url in files[:1]: # Webex API allows only a single file attachment
                url_loaded = False
                count = 0
                while not url_loaded and count < MAX_URL_RETRIES:
                    logger.debug(f"loading {url}")
                    head_response = http.request("HEAD", url, headers=file_headers)
                    logger.debug(f"HEAD response headers: {head_response.getheaders()}")
                    get_response = http.request("GET", url, headers=file_headers, preload_content=False)
                    logger.debug(f"GET response headers: {get_response.getheaders()}")
                    retry_after = float(get_response.getheader("retry-after", 0))
                    if retry_after > 0:
                        logger.info(f"file not ready, retry after {retry_after} seconds")
                        time.sleep(retry_after)
                        count += 1
                    else:
                        responses.append(get_response)
                        logger.debug(f"loaded {url}")
                        url_loaded = True
                
            msg_data["roomId"] = room_id
                
            for response in responses:
                disp = response.getheader("content-disposition")
                file_name = re.findall(r"^attachment;.*filename=\"(.*)\"", disp)[0]
                content_type = response.getheader("Content-Type")
                logger.debug(f"received \"{file_name}\" of \"{content_type}\"")
                send_as_file = True
                if content_type == "application/json":
                    send_as_file = False
                    logger.debug(f"JSON file {file_name} detected, trying to create an adaptive card")
                    reader = codecs.getreader("utf-8")
                    try:
                        form = json.loads(response.data)
                        attachment_msg = msg_data.copy()
                        attachment_msg["attachments"] = [wrap_form(form)]
                        attachment_msg.pop("files", None) # make sure there is no file attachment - mutually exclusive with "attachments"
                        attachment_msg["markdown"] = "Form attached"
                        try:
                            result = webex_api.messages.create(**attachment_msg)
                            logger.debug(f"adaptive card send result: {result}")
                        except ApiError as e:
                            logger.error(f"create message with adaptive card failed: {e}.")
                            send_as_file = True
                    except Exception as e:
                        logger.error(f"create adaptive card error: {e}")
                        send_as_file = True
                if send_as_file:
                    msg_data["files"] = (file_name, response.data, content_type) # Webex API allows only a single file attachment
                
                    # logger.debug(f"sending to Webex API: {msg_data}")
                    multipart_data = MultipartEncoder(msg_data)
                    multi_headers = {'Content-type': multipart_data.content_type}
                    logger.debug(f"multipart headers: {multi_headers}")

                    try:
                        json_data = webex_api.messages._session.post('messages', data=multipart_data, headers=multi_headers)
                        result = webex_api.messages._object_factory('message', json_data)
                        logger.debug(f"message with file created: {result}")
                    except Exception as e:
                        logger.error(f"create message with attachment failed: {e}")

            for response in responses:
                response.release_conn()            
        else:
            result = webex_api.messages.create(roomId = room_id, **msg_data)
        
        return result
    except ApiError as e:
        logger.error(f"Create message failed: {e}.")
        
def check_sender(sender_info, bot_info, config):
    result = True
    if config["source"]["bots_own_org"]:
        logger.debug(f"check sender & bot orgId: {sender_info.orgId == bot_info.orgId}")
        result = sender_info.orgId == bot_info.orgId
    if config["source"]["from_sender_list"]:
        logger.debug(f'check sender in sender_list: {sender_info.emails[0] in config["source"]["sender_list"]}')
        result &= sender_info.emails[0] in config["source"]["sender_list"]
        
    logger.debug(f"check sender result: {result}")
    return result
        
def check_destination(room_id, sender_info, bot_info, config):
    result = check_sender(sender_info, bot_info, config)
    if result and (config["destination"]["bots_own_org"] or config["destination"]["senders_own_org"]):
        try:
            room_info = webex_api.rooms.get(room_id)
            logger.debug(f"destination room info: {room_info}")
            if config["destination"]["bots_own_org"]:
                logger.debug(f"check room owner orgId and bot's orgId: {room_info.ownerId == bot_info.orgId}")
                result = room_info.ownerId == bot_info.orgId
            if config["destination"]["senders_own_org"]:
                logger.debug(f"check room owner orgId and sender's orgId: {room_info.ownerId == sender_info.orgId}")
                result &= room_info.ownerId == sender_info.orgId
        except ApiError as e:
            logger.error(f"get room info failed: {e}.")
            return False
    logger.debug(f"final result: {result}")
    return result
    
def check_membership(room_info, bot_info, config):
    result = True
    if config["membership"]["bots_own_org"]:
        result = room_info.ownerId == bot_info.orgId
    
    return result

async def manage_webhooks(target_url):
    """create a set of webhooks for the Bot
    webhooks are defined according to the resource_events dict
    
    arguments:
    target_url -- full URL to be set for the webhook
    """
    myUrlParts = urlparse(target_url)
    target_url = secure_scheme(myUrlParts.scheme) + "://" + myUrlParts.netloc + url_for("webex_webhook")

    logger.debug("Create new webhook to URL: {}".format(target_url))
    
    resource_events = {
        "messages": ["created"],
        "memberships": ["created", "deleted", "updated"],
        "rooms": ["updated"]
        # "attachmentActions": ["created"]
    }
    status = None
        
    try:
        check_webhook = webex_api.webhooks.list()
    except ApiError as e:
        logger.error("Webhook list failed: {}.".format(e))

    local_loop = asyncio.get_event_loop()

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        wh_task_list = []
        for webhook in check_webhook:
            wh_task_list.append(local_loop.run_in_executor(executor, delete_webhook, webhook))
            
        await asyncio.gather(*wh_task_list)
                
        wh_task_list = []
        for resource, events in resource_events.items():
            for event in events:
                wh_task_list.append(local_loop.run_in_executor(executor, create_webhook, resource, event, target_url))
                
        result = True
        for status in await asyncio.gather(*wh_task_list):
            if not status:
                result = False
                
    return result
    
def delete_webhook(webhook):
    logger.debug(f"Deleting webhook {webhook.id}, '{webhook.id}', App Id: {webhook.appId}")
    try:
        if not flask_app.testing:
            logger.debug(f"Start webhook {webhook.id} delete")
            webex_api.webhooks.delete(webhook.id)
            logger.debug(f"Webhook {webhook.id} deleted")
    except ApiError as e:
        logger.error("Webhook {} delete failed: {}.".format(webhook.id, e))

def create_webhook(resource, event, target_url):
    logger.debug(f"Creating for {resource,event}")
    status = False
    try:
        if not flask_app.testing:
            result = webex_api.webhooks.create(name="Webhook for event \"{}\" on resource \"{}\"".format(event, resource), targetUrl=target_url, resource=resource, event=event)
        status = True
        logger.debug(f"Webhook for {resource}/{event} was successfully created with id: {result.id}")
    except ApiError as e:
        logger.error("Webhook create failed: {}.".format(e))
        
    return status
    
def secure_scheme(scheme):
    return re.sub(r"^http$", "https", scheme)
    
def load_config(default_config_file = "default_config.json", user_config_file = "config/config.json"):
    try:
        with open(default_config_file) as cfg_file:
            config = json.load(cfg_file)
    except Exception as e:
        logger.error(f"default configuration load failed: {e}")
        config = DEFAULT_CONFIG
        
    try:
        cfg = os.getenv("CONFIG_FILE", user_config_file)
        logger.debug(f"user config file: {cfg}")
        with open(cfg) as cfg_file:
            user_config = json.load(cfg_file)
            logger.debug(f"user configuration: {user_config}")
            
        config = config | user_config
    except Exception as e:
        logger.error(f"user configuration load failed: {e}")

    logger.debug(f"current configuration: {config}")
    return config
    
def wrap_form(form):
    card = EMPTY_CARD
    card["content"] = form
    
    return card

"""
Independent thread startup, see:
https://networklore.com/start-task-with-flask/
"""
def start_loop():
    no_proxies = {
      "http": None,
      "https": None,
    }
    while True:
        logger.debug('In start loop')
        try:
            resp = requests.get(f"https://127.0.0.1:{PORT}/startup", proxies=no_proxies, verify=False)
            logger.debug(f"Response status: {resp.status_code}, OK: {resp.ok}")
            if resp.ok:
                logger.info('Server started, quiting start_loop')
                break
        except Exception as e:
            logger.info(f'Server not yet started, {e}')
        time.sleep(2)

def start_runner():
    logger.debug('Start runner')
    start_loop()
    logger.debug('End runner')

def signal_handler(signal, frame):
    loop.stop()
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)

    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--verbose', action='count', help="Set logging level by number of -v's, -v=WARN, -vv=INFO, -vvv=DEBUG")
    
    args = parser.parse_args()
    if args.verbose:
        if args.verbose > 2:
            logging.basicConfig(level=logging.DEBUG)
        elif args.verbose > 1:
            logging.basicConfig(level=logging.INFO)
        if args.verbose > 0:
            logging.basicConfig(level=logging.WARN)
            
    logger.info("Logging level: {}".format(logging.getLogger(__name__).getEffectiveLevel()))
    
    bot_identity = webex_api.people.me()
    logger.info(f"Bot \"{bot_identity.displayName}\" starting...")
    
    _thread.start_new_thread(start_runner, ())
    
    flask_app.run(host="0.0.0.0", port=PORT, debug=True, threaded=True, use_reloader=True, reloader_type="watchdog", ssl_context='adhoc')
