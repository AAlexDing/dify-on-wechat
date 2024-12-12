"""
发送消息到微信的插件
@Version: 2.3
@Description: 实现了一个监听文件变化发送消息的插件，可以通过微信命令/api接口请求 发送消息到微信
@Author: Isaac
@Date: 2024-05-24
@Update: 2024-09-05
@更新日志:
    2024-09-05: 2.3版本
        1.增加channel判断，兼容win版本机器人的ntchat发送消息（https://github.com/Tishon1532/chatgpt-on-wechat-win），
        支持itchat和ntchat两种channel类型，注意ntchat目前还没解决群聊@所有人的场景，只能@单个人，另外ntchat的receiver_name只支持填写微信名字
    2024-08-15: 2.2版本
        1.修改了插件名字和文件夹一致
        2.api服务修改逻辑，不注册插件，采用函数调用的方式启动api服务
        ps:注意因为此次修改了插件名称，之前/plugins/plugins.json目录下的file_writer和file_watcher两个插件名字需要删除
    2024-07-19: 2.1版本
        1.优化兼容发送好友消息，先查找微信备注名，找不到再查找微信昵称，receiver_name支持填写微信备注名和微信昵称。
        2.优化了发送群聊消息的逻辑，之前一定要加好友才能@指定人，现在不需要加好友也可以@指定人。
        （先从群聊里找微信名，找不到通过好友列表找微信备注名，备注名没有再找微信昵称）
    2024-07-18: 2.0版本
        1.新增支持发送图片、视频和文件的消息格式（文件内容传参http或者https的url）
    2024-05-29: 1.5版本，
        1.新增支持微信命令发送消息功能。
        2.优化了文件监听的模式，使用了python看门狗模式监听文件变化
        3.更新了文档，添加了更多的使用示例。
        4.修复了一些已知的 bug。
    2024-05-24: 1.0版本
        1.初始插件版本发布，支持基本api触发消息发送微信功能。
        2.提供了简单的配置选项和说明文档。
@联系作者：微信号：shine86869 （备注ai插件）
"""

import requests
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from plugins import *
import plugins
from plugins.send_msg.file_api import FileWriter
from config import conf
import time


class FileChangeHandler(FileSystemEventHandler):
    def __init__(self, callback):
        self.callback = callback

    def on_modified(self, event):
        if event.src_path.endswith('data.json'):
            self.callback()


@plugins.register(
    name="send_msg",
    desire_priority=180,
    hidden=True,
    desc="watchdog监听文件变化发送消息&微信命令发送消息",
    version="2.3",
    author="Isaac"
)
class FileWatcherPlugin(Plugin):
    def __init__(self):
        super().__init__()
        self.channel = None
        self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
        curdir = os.path.dirname(__file__)
        FileWriter()  # 启动api服务
        self.file_path = os.path.join(curdir, "data.json")
        self.observer = Observer()
        self.event_handler = FileChangeHandler(self.handle_message)
        self.start_watch()  # 默认启动 watchdog 监听

        # 缓存相关属性
        self._contacts_cache = None
        self._rooms_brief_info_cache = None 
        self._friends_brief_info_cache = None
        self._room_members_cache = {}  # {room_wxid: {"members": [...], "update_time": timestamp}}
        
        # 不同类型缓存的更新时间
        self._contacts_update_time = {
            "all": 0,  # 完整联系人列表的更新时间
            "rooms": 0,  # 群聊缓存的更新时间
            "friends": 0  # 好友缓存的更新时间
        }
        
        # 缓存过期时间设置
        self._cache_expire_seconds = 86400  # 联系人缓存过期时间
        self._room_members_expire_seconds = 86400  # 群成员缓存过期时间

        # 根据配置获取当前的channel类型
        self.channel_type = conf().get("channel_type", "wx")
        if self.channel_type == "wx":
            try:
                from lib import itchat
                self.channel = itchat
            except Exception as e:
                logger.error(f"未安装itchat: {e}")
        elif self.channel_type == "ntchat":
            try:
                from channel.wechatnt.ntchat_channel import wechatnt
                self.channel = wechatnt
            except Exception as e:
                logger.error(f"未安装ntchat: {e}")
        elif self.channel_type == "gewechat":
            try:
                from lib.gewechat import GewechatClient
                self.base_url = conf().get("gewechat_base_url")
                self.token = conf().get("gewechat_token")
                self.app_id = conf().get("gewechat_app_id")
                self.channel = GewechatClient(self.base_url, self.token)
                self._update_contacts_cache("all")
            except Exception as e:
                logger.error(f"未安装gewechat: {e}")
        else:
            logger.error(f"不支持的channel_type: {self.channel_type}")
            
    def _update_contacts_cache(self, update_type="all"):
        """
        更新联系人缓存
        @param update_type: 更新类型，可选值：
            - "all": 更新所有内容(默认)
            - "rooms": 只更新群聊
            - "friends": 只更新好友
        """
        current_time = time.time()
        
        # 根据更新类型和缓存状态决定是否需要更新
        if update_type == "all":
            if (self._contacts_cache and 
                current_time - self._cache_update_time < self._cache_expire_seconds):
                return
        elif update_type == "rooms":
            if (self._rooms_brief_info_cache and 
                current_time - self._cache_update_time < self._cache_expire_seconds):
                return
        elif update_type == "friends":
            if (self._friends_brief_info_cache and 
                current_time - self._cache_update_time < self._cache_expire_seconds):
                return
        else:
            raise ValueError(f"不支持的更新类型: {update_type}")

        # 获取联系人列表
        contacts = self.channel.fetch_contacts_list(self.app_id)
        if contacts.get("ret") != 200:
            raise ValueError(f"获取联系人列表失败: {contacts.get('msg')}")
        
        self._contacts_cache = contacts.get("data", {})
        
        # 根据更新类型处理不同的缓存更新
        if update_type in ["all", "rooms"]:
            # 更新群聊缓存
            rooms = self._contacts_cache.get("chatrooms", [])
            rooms_brief_info = []
            for i in range(0, len(rooms), 100):
                rooms_brief_info.extend(
                    self.channel.get_brief_info(
                        self.app_id, 
                        rooms[i:i+100]
                    ).get("data", [])
                )
            self._rooms_brief_info_cache = rooms_brief_info
            
        if update_type in ["all", "friends"]:
            # 更新好友缓存
            friends = self._contacts_cache.get("friends", [])
            friends_brief_info = []
            for i in range(0, len(friends), 100):
                friends_brief_info.extend(
                    self.channel.get_brief_info(
                        self.app_id, 
                        friends[i:i+100]
                    ).get("data", [])
                )
            self._friends_brief_info_cache = friends_brief_info
        
        # 更新缓存时间
        self._cache_update_time = current_time 
    def _get_room_members(self, room_wxid):
        """获取群成员列表(带缓存)"""
        current_time = time.time()
        cache_info = self._room_members_cache.get(room_wxid)
        
        # 检查缓存是否存在且未过期
        if (cache_info and 
            current_time - cache_info["update_time"] < self._room_members_expire_seconds):
            return cache_info["members"]
        
        # 缓存不存在或已过期,重新获取
        members = self.channel.get_chatroom_member_list(self.app_id, room_wxid)
        if members.get("ret") == 200:
            members = members.get("data", {})
            member_list = members.get("memberList", [])
            # 更新缓存
            self._room_members_cache[room_wxid] = {
                "members": member_list,
                "update_time": current_time
            }
            return member_list
        else:
            raise ValueError(f"获取群成员列表失败: {members.get('msg')}")

        
    def on_handle_context(self, e_context: EventContext):
        if e_context['context'].type != ContextType.TEXT:
            return
        content = e_context['context'].content
        if content == "$start watchdog":
            self.start_watch()
            e_context.action = EventAction.BREAK_PASS
            reply = Reply()
            reply.type = ReplyType.INFO
            reply.content = "watchdog started."
            e_context['reply'] = reply
            self.handle_message()  # 处理文件中的现有数据
        elif content == "$stop watchdog":
            self.stop_watch()
            e_context.action = EventAction.BREAK_PASS
            reply = Reply()
            reply.type = ReplyType.INFO
            reply.content = "watchdog stopped."
            e_context['reply'] = reply
        elif content == "$check watchdog":
            if self.observer.is_alive():
                reply = Reply()
                reply.type = ReplyType.INFO
                reply.content = "watchdog 正在运行,如需停止:关闭命令 $stop watchdog."
            else:
                reply = Reply()
                reply.type = ReplyType.INFO
                reply.content = "watchdog 没在运行,如需启动:启动命令 $start watchdog."
            e_context['reply'] = reply
            e_context.action = EventAction.BREAK_PASS
        elif content.startswith("$send_msg"):
            try:
                # 提取接收者名称列表
                receiver_start = content.find('[')
                receiver_end = content.find(']', receiver_start)
                if receiver_start != -1 and receiver_end != -1:
                    receiver_names = [name.strip() for name in content[receiver_start + 1:receiver_end].split(',')]
                    content = content[receiver_end + 1:].strip()
                else:
                    receiver_names = []

                # 提取群聊名称列表
                group_start = content.find('group[')
                if group_start != -1:
                    group_end = content.find(']', group_start)
                    if group_end != -1:
                        group_names = [name.strip() for name in content[group_start + 6:group_end].split(',')]
                        content = content[:group_start].strip()
                    else:
                        group_names = []
                else:
                    group_names = []

                # 判断是否@所有人
                if "所有人" in receiver_names or "all" in receiver_names:
                    receiver_names = ["所有人"]

                self.send_message(receiver_names, content, group_names)

                reply = Reply()
                reply.type = ReplyType.INFO
                reply.content = "消息发送成功."
                e_context['reply'] = reply
            except Exception as e:
                reply = Reply()
                reply.type = ReplyType.ERROR
                reply.content = f"消息发送失败: {str(e)}"
                e_context['reply'] = reply
            e_context.action = EventAction.BREAK_PASS

    def start_watch(self):
        if not self.observer.is_alive():
            self.observer = Observer()
            self.observer.schedule(self.event_handler, path=os.path.dirname(self.file_path), recursive=False)
            self.observer.start()
            logger.info("watchdog started.")
        else:
            logger.info("watchdog is already running.")

    def stop_watch(self):
        if self.observer.is_alive():
            self.observer.stop()
            self.observer.join()
            logger.info("watchdog stopped.")
        else:
            logger.info("watchdog is not running.")

    def handle_message(self):
        try:
            with open(self.file_path, 'r', encoding='utf-8') as file:
                data = file.read().strip()
                if data:  # 判断文件内容是否为空
                    data_list = json.loads(data)
                    for data in data_list:
                        self.process_message(data)
                    with open(self.file_path, 'w', encoding='utf-8') as file:
                        file.write('')
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"读取文件 {self.file_path} 出错: {e}")

    def process_message(self, data):
        try:
            receiver_name = data["receiver_name"]  # 获取接收者名称
            content = data["message"]  # 获取消息内容
            group_name = data["group_name"]  # 获取群聊名称

            self.send_message(receiver_name, content, group_name)
        except Exception as e:
            logger.error(f"处理消息时发生异常: {e}")

    def send_message(self, receiver_names, content, group_names=None):
        """
        发送消息，根据channel_type选择使用ntchat或itchat
        如果type是wx使用itchat，如果是ntchat使用ntchat
        :param receiver_names: 接收者名称列表
        :param content: 消息内容
        :param group_names: 群聊名称列表
        """
        try:
            if self.channel_type == "wx":
                self._send_itchat_message(receiver_names, content, group_names)
            elif self.channel_type == "ntchat":
                self._send_ntchat_message(receiver_names, content, group_names)
            elif self.channel_type == "gewechat":
                self._send_gewechat_message(receiver_names, content, group_names)
            else:
                raise ValueError(f"不支持的channel_type: {self.channel_type}")
        except Exception as e:
            logger.error(f"处理消息时发生异常: {e}")
            raise e
        
    def _send_group_message(self, group_name, wxid, receiver_names, content, media_type):
        """处理群聊消息发送的逻辑"""
        if receiver_names and any(receiver_names):
            if "所有人" in receiver_names or "all" in receiver_names:
                at_content = "@所有人"
                self.channel.post_text(self.app_id, wxid, content, at_content)
                logger.info(f"发送群聊消息成功, 群聊: {group_name}, @所有人, 消息: {content}")
            else:
                member_list = self._get_room_members(wxid)
                
                at_wxids = []
                for member in member_list:
                    if member.get("nickName") in receiver_names:
                        at_wxids.append(member.get("wxid"))
                
                if at_wxids:
                    at_content = ",".join(at_wxids)
                    self.channel.post_text(self.app_id, wxid, content, at_content)
                    logger.info(f"发送群聊消息成功, 群聊: {group_name}, @用户: {receiver_names}, 消息: {content}")
                else:
                    # 如果找不到成员,尝试刷新群成员缓存再找一次
                    if wxid in self._room_members_cache:
                        del self._room_members_cache[wxid]
                    member_list = self._get_room_members(wxid)
                    
                    at_wxids = []
                    for member in member_list:
                        if member.get("nickName") in receiver_names:
                            at_wxids.append(member.get("wxid"))
                    
                    if at_wxids:
                        at_content = ",".join(at_wxids)
                        self.channel.post_text(self.app_id, wxid, content, at_content)
                        logger.info(f"发送群聊消息成功, 群聊: {group_name}, @用户: {receiver_names}, 消息: {content}")
                    else:
                        logger.warning(f"在群 {group_name} 中未找到指定的成员: {receiver_names}")
        else:
            self._send_gewechat_media_or_text(media_type, content, wxid)
            logger.info(f"发送群聊消息成功, 群聊: {group_name}, 消息: {content}")
            
            
    def _send_gewechat_message(self, receiver_names, content, group_names):
        """使用 gewechat 发送消息的逻辑"""
        try:
            # 判断消息类型
            if content.startswith(("http://", "https://")):
                if content.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".img")):
                    media_type = "img"
                elif content.lower().endswith((".mp4", ".avi", ".mov", ".pdf")):
                    media_type = "video"
                elif content.lower().endswith((".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar", "txt")):
                    media_type = "file"
                else:
                    logger.warning(f"不支持的文件类型: {content}")
                    return
            else:
                media_type = "text"
            # 过滤空名群聊
            group_names = [group_name for group_name in group_names if group_name]
            
            # 处理群聊消息
            if group_names:
                for group_name in group_names:
                    self._update_contacts_cache("rooms")
                    room_found = False
                    
                    for room in self._rooms_brief_info_cache:
                        if room.get("nickName") == group_name:
                            room_found = True
                            wxid = room.get("userName")
                            self._send_group_message(group_name, wxid, receiver_names, content, media_type)
                            break

                    if not room_found:
                        # 如果缓存中找不到,强制更新缓存后再找一次
                        self._contacts_update_time["rooms"] = 0  # 只重置群聊缓存时间
                        self._update_contacts_cache("rooms")
                        
                        for room in self._rooms_brief_info_cache:
                            if room.get("nickName") == group_name:
                                room_found = True
                                wxid = room.get("userName")
                                self._send_group_message(group_name, wxid, receiver_names, content, media_type)
                                break
                                
                        if not room_found:
                            raise ValueError(f"未找到群聊: {group_name}")

            # 处理私聊消息
            else:
                for receiver_name in receiver_names:
                    self._update_contacts_cache()
                    contact_found = False
                    
                    for friend in self._friends_brief_info_cache:
                        if friend.get("nickName") == receiver_name or friend.get("remark") == receiver_name:
                            contact_found = True
                            wxid = friend.get("userName")
                            self._send_gewechat_media_or_text(media_type, content, wxid)
                            logger.info(f"发送私聊消息成功, 接收者: {receiver_name}, 消息: {content}")
                            break
                    
                    if not contact_found:
                        # 如果缓存中找不到,强制更新缓存后再找一次
                        self._cache_update_time = 0
                        self._update_contacts_cache()
                        
                        for friend in self._friends_brief_info_cache:
                            if friend.get("nickName") == receiver_name or friend.get("remark") == receiver_name:
                                contact_found = True
                                wxid = friend.get("userName")
                                self._send_gewechat_media_or_text(media_type, content, wxid)
                                logger.info(f"发送私聊消息成功, 接收者: {receiver_name}, 消息: {content}")
                                break
                                
                        if not contact_found:
                            raise ValueError(f"未找到联系人: {receiver_name}")

        except Exception as e:
            logger.error(f"发送gewechat消息时发生异常: {e}")
            raise e


    def _send_gewechat_media_or_text(self, media_type, content, wxid):
        """
        根据消息类型发送不同格式的消息
        """
        if media_type == "text":
            self.channel.post_text(self.app_id, wxid, content, "")
        elif media_type == "img":
            pass
            # self.channel.post_image(self.app_id, wxid, content)
        elif media_type == "video":
            # 视频需要缩略图,这里暂时传空字符串
            pass
            # self.channel.post_video(self.app_id, wxid, content, "", 0)
        elif media_type == "file":
            pass
            # filename = os.path.basename(content)
            # self.channel.post_file(self.app_id, wxid, content, filename)
        else:
            logger.error(f"不支持的消息类型: {media_type}")

    def _send_itchat_message(self, receiver_names, content, group_names):
        """
        使用 itchat 发送消息的逻辑
        """
        global media_type, content_at
        try:
            # 更新 itchat 的内部缓存
            self.channel.get_friends(update=True)
            self.channel.get_chatrooms(update=True)

            # 判断消息类型
            if content.startswith(("http://", "https://")):
                if content.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".img")):
                    media_type = "img"
                elif content.lower().endswith((".mp4", ".avi", ".mov", ".pdf")):
                    media_type = "video"
                elif content.lower().endswith((".doc", ".docx", ".xls", "xlsx", ".zip", ".rar", "txt")):
                    media_type = "file"
                else:
                    logger.warning(f"不支持的文件类型: {content}")
            else:
                media_type = "text"

            if group_names:
                for group_name in group_names:
                    chatrooms = self.channel.search_chatrooms(name=group_name)
                    if not chatrooms:
                        raise ValueError(f"没有找到对应的群聊：{group_name}")
                    chatroom = chatrooms[0]

                    if receiver_names and any(receiver_names):
                        for receiver_name in receiver_names:
                            if receiver_name == "所有人":
                                content_at = f"@所有人 "
                            else:
                                # 先去群聊找对应的成员，找不到再去好友列表找（先用微信备注名查找，找不到用微信名）
                                member_found = False
                                for member in chatroom.MemberList:
                                    if member.NickName == receiver_name or member.DisplayName == receiver_name:
                                        content_at = f"@{member.NickName} "
                                        member_found = True
                                        break
                                if not member_found:
                                    friends = self.channel.search_friends(remarkName=receiver_name)
                                    if not friends:
                                        friends = self.channel.search_friends(name=receiver_name)
                                    if friends:
                                        content_at = f"@{friends[0].NickName} "
                                        member_found = True
                                if not member_found:
                                    raise ValueError(f"在群聊 {group_name} 中没有找到对应的成员：{receiver_name}")
                            self.send_msg(msg_type=media_type, content=content,
                                          to_user_name=chatroom.UserName, at_content=content_at)
                            logger.info(
                                f"手动发送微信群聊消息成功, 发送群聊:{group_name}, 接收者:{receiver_name}, 消息内容：{content}")
                    else:
                        self.send_msg(media_type, content, chatroom.UserName)
                        logger.info(f"手动发送微信群聊消息成功, 发送群聊:{group_name}, 消息内容：{content}")
            else:
                if receiver_names and any(receiver_names):
                    for receiver_name in receiver_names:
                        friends = self.channel.search_friends(remarkName=receiver_name)
                        if not friends:
                            friends = self.channel.search_friends(name=receiver_name)
                        if friends:
                            self.send_msg(media_type, content, friends[0].UserName)
                            logger.info(f"手动发送微信消息成功, 发送人:{friends[0].NickName} 消息内容：{content}")
                        else:
                            raise ValueError(f"没有找到对应的好友：{receiver_name}")
                else:
                    raise ValueError("接收者列表为空,无法发送个人消息")
        except Exception as e:
            logger.error(f"处理消息时发生异常: {e}")
            raise e

    def _send_ntchat_message(self, receiver_names, content, group_names):
        """
        使用 ntchat 发送消息的逻辑
        """
        try:
            if content.startswith(("http://", "https://")):
                if content.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".img")):
                    media_type = "img"
                elif content.lower().endswith((".mp4", ".avi", ".mov", ".pdf")):
                    media_type = "video"
                elif content.lower().endswith((".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar", "txt")):
                    media_type = "file"
                else:
                    logger.warning(f"不支持的文件类型: {content}")
                    return
            else:
                media_type = "text"

            # 处理群聊消息
            if group_names:
                for group_name in group_names:
                    rooms = self.channel.get_rooms()  # 获取所有群聊
                    room_status = False

                    for room in rooms:
                        if group_name == room.get("nickname"):
                            room_status = True
                            logger.info(f"找到对应的群聊: {room}")
                            wxid = room.get("wxid")
                            room_members = self.channel.get_room_members(wxid)
                            logger.info(f"{wxid}, room_members: {room_members}")
                            user_wxids = []

                            if receiver_names and any(receiver_names):
                                # 检查是否是@所有人
                                if "所有人" in receiver_names or "all" in receiver_names:
                                    logger.info(f"发送群聊消息，@所有人: {content}")
                                    at_content = f"@所有人 {content}"
                                    self.channel.send_room_at_msg(wxid, at_content, [])
                                    logger.info(
                                        f"成功发送群聊消息, 群聊: {group_name}, 消息: {at_content},接收人: 所有人")
                                else:
                                    # 查找群成员并构造 @ 的内容
                                    for room_member in room_members["member_list"]:
                                        for receiver_name in receiver_names:
                                            if room_member.get("nickname") == receiver_name:
                                                logger.info(f"找到对应的群成员: {receiver_name}")
                                                user_wxid = room_member.get("wxid")
                                                user_wxids.append(user_wxid)

                                    member_n = len(receiver_names)
                                    if len(user_wxids) == member_n:
                                        # 构造 @ 消息的内容
                                        at_content = f"{' '.join([f'@{receiver_name}' for receiver_name in receiver_names])} {content}"
                                        logger.info(f"找到所有对应的群成员，发送消息内容: {at_content}")
                                        self.channel.send_room_at_msg(wxid, at_content, user_wxids)
                                        logger.info(
                                            f"send_room_at_msg: wxid: {wxid}, content: {at_content},user_wxids: {user_wxids}，群聊名称: {group_name}")
                                    else:
                                        logger.warning(f"未找到所有指定的成员: {receiver_names}")
                            else:
                                # receiver_names为空时发送普通消息
                                self.channel.send_text(wxid, content)
                                logger.info(f"发送普通群聊消息，没有@成员: {content}")
                            break

                    if not room_status:
                        logger.warning(f"未找到对应的群聊: {group_name}")

            # 处理单聊消息
            else:
                for receiver_name in receiver_names:
                    wxid = self._find_friend_by_name(receiver_name)
                    if wxid:
                        self._send_ntchat_media_or_text(media_type, content, wxid)
                        logger.info(f"成功发送单聊消息, 接收人: {receiver_name}, 消息: {content}")
                    else:
                        raise ValueError(f"没有找到对应的好友：{receiver_name}")

        except Exception as e:
            logger.error(f"发送ntchat消息时发生异常: {e}")
            raise e

    def _find_chatroom_by_name(self, group_name):
        """
        根据群聊名称查找群聊
        """
        rooms = self.channel.get_rooms()  # 获取所有群聊
        for room in rooms:
            if room["nickname"] == group_name:
                return room
        return None

    def _find_member_in_chatroom(self, group_wxid, member_name):
        """
        根据成员名称在群聊中查找成员wxid
        """
        room_members = self.channel.get_room_members(group_wxid)
        for member in room_members["member_list"]:
            if member["nickname"] == member_name:
                return member["wxid"]
        return None

    def _find_friend_by_name(self, friend_name):
        """
        根据好友名称查找wxid
        """
        friends = self.channel.get_contacts()  # 获取所有好友
        for friend in friends:
            if friend["nickname"] == friend_name or friend["remark"] == friend_name:
                return friend["wxid"]
        return None

    def _send_ntchat_media_or_text(self, media_type, content, wxid):
        """
        ntchat根据消息类型发送文本、图片、视频或文件
        """
        if media_type == "text":
            self.channel.send_text(wxid, content)
        elif media_type == "img":
            image_path = self.download_file(content)
            self.channel.send_image(wxid, image_path)
        elif media_type == "video":
            video_path = self.download_file(content)
            self.channel.send_video(wxid, video_path)
        elif media_type == "file":
            file_path = self.download_file(content)
            self.channel.send_file(wxid, file_path)
        else:
            logger.error(f"不支持的消息类型: {media_type}")

    def send_msg(self, msg_type, content, to_user_name, at_content=None):
        """
        实际itchat发送消息函数
        :param msg_type: 消息类型
        :param content: 消息内容
        :param to_user_name: 接收者的 UserName
        :param at_content: @的内容
        """
        if msg_type == 'text':
            if at_content:
                self.channel.send(f'{at_content} {content}', to_user_name)
            else:
                self.channel.send(content, to_user_name)
        elif msg_type in ['img', 'video', 'file']:
            # 如果是图片、视频或文件,先下载到本地
            local_file_path = self.download_file(content)
            if local_file_path:
                self.channel.send(at_content, to_user_name)
                if msg_type == 'img':
                    self.channel.send_image(local_file_path, to_user_name)
                elif msg_type == 'video':
                    self.channel.send_video(local_file_path, to_user_name)
                elif msg_type == 'file':
                    self.channel.send_file(local_file_path, to_user_name)
                # 发送完成后删除本地临时文件
                os.remove(local_file_path)
            else:
                raise ValueError(f"无法下载文件: {content}")

    def download_file(self, url):
        """
        下载文件到本地
        :param url: 文件的URL
        """
        try:
            response = requests.get(url)
            if response.status_code == 200:
                file_name = os.path.basename(url)
                with open(file_name, 'wb') as file:
                    file.write(response.content)
                return file_name
            return None
        except Exception as e:
            logger.error(f"下载文件时发生异常: {e}")
            return None

    def get_help_text(self, **kwargs):
        return ("1. watchdog监听文件变化插件,监听data.json文件变化发送微信通知.(默认启动)\n"
                "启动监听: $start watchdog\n停止监听: $stop watchdog\n查看监听状态: $check watchdog\n\n"
                "2. 微信命令发送消息: \n"
                "$send_msg [微信���注名1,微信备注名2] 消息内容\n"
                "$send_msg [微信备注名1,微信备注名2] 消息内容 group[群聊名称1,群聊名称2]\n"
                "$send_msg [所有人] 消息内容 group[群聊名称1,群聊名称2]")
