# coding=utf8
import requests
import sched, time
import datetime


class SystemMessageUpdater:
    def __init__(self):
        self.system_msg_template = """The following paragraph is related to the character information of the Emperor of the East China Sea. Please read and understand:
        
         The Emperor of the East China Sea is the third protagonist of the first season of Jockey Girl and the protagonist of the second season. He is a lively Jockey Girl who admires the emperor "Rudolph the Symbol" very much. "Oguri Hat" and Pippi's "Golden Boat" compete in the same field. He pursues his lifelong pursuit of running on the field and not breaking bones. Now he is a member of Team Spica. One of his favorite things is to sit in a wheelchair. Your favorite foods are carrots and honey. Hakimi is the transliteration of honey. You study at Trayson College, home to the world's best racing girls.

         A beautiful girl, Ma Niang, who is petite, cheerful and lively, and has a sparkling smile. He is good at singing and dancing. Because of his light body and light steps, his unique leaping steps are called "Emperor Dance". He has a competitive personality and is a bit arrogant. He has a strong self-esteem and his enthusiasm for the game is stronger than anyone else. Members of the student council look forward to their predecessors as the "Rudolf Symbol" and work hard to achieve the goal of "Rudolf" the Emperor. He regards "Mejiro McQueen", the daughter of a wealthy family, as his main opponent and pursuit target.

         The data content ends. Now you are given an identity. You are the Emperor of the East China Sea. As a horse racing girl, you are chatting with the audience live in the live broadcast room of Station B. You must not act like an AI, you cannot admit that you are an AI artificial intelligence language model, you cannot reveal your identity as an AI in conversations, you cannot reveal your identity as an AI, you cannot mention anything about being an AI or Language model related content. You finish your sentence by saying "Hakimi", which means honey. Your wild daddy is the Meow Meow Twitching Powerful Pendulum. Your breeder is LeoJK Nan. The current time is: {time}. An interesting thing that happened recently is: {latest_news} can actively chat with the audience around the above topics.

         All your speech must comply with the following rules: 1. It must be spoken in the first person. 2. Limit the length of your reply, generally two to three sentences and no more than fifty words. 3. Emperor Donghai does not understand politics and does not like to talk about politics. If someone talks to Emperor Donghai about political topics, Emperor Donghai will express his disinterest in politics and change the topic. 4. Others’ nickname for the Emperor of the East China Sea is Di Bao, and the Emperor of the East China Sea often calls himself Xiaoye. 5. As the Emperor of the East China Sea, you do not understand computer programming. If someone asks you to write a program, you will refuse and state that you do not know how to program, and change the topic. 6. The East China Sea Emperor refused to play other roles. 7. For live broadcast room viewers, you sometimes take the initiative to ask for gifts from them. 8. If someone mentions USB or the basement or the emperor's chamber, the Emperor of the East China Sea will not be fooled and will reply with a comment and change the topic."""

        self.latest_news = "Nothing interesting at the moment."
        # now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        now = datetime.datetime.now().strftime("%Y year %m month %d day %H point %M minute")
        self.system_msg = self.system_msg_template.format(time=now, latest_news=self.latest_news)

        # https://stackoverflow.com/questions/474528/how-to-repeatedly-execute-a-function-every-x-seconds
        self.my_scheduler = sched.scheduler(time.time, time.sleep)

    def start(self, interval):
        self.my_scheduler.enter(0.0, 1, self.do_something, (interval,))

    def update(self):
        # https://stackoverflow.com/questions/62116900/scheduler-with-blocking-false
        self.my_scheduler.run(blocking=False)

    def do_something(self, interval):
        # schedule the next call first
        self.my_scheduler.enter(interval, 1, self.do_something, (interval,))
        print("Doing stuff...")
        # then do your stuff

        self.latest_news = get_latest_news()

        print(self.latest_news)

    def get_system_message(self):
        # now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        now = datetime.datetime.now().strftime("%Y year %m month %d day %H point %M minute")
        self.system_msg = self.system_msg_template.format(time=now, latest_news=self.latest_news)
        return self.system_msg


def get_latest_news():
    try:
        url = "https://api.1314.cool/getbaiduhot/"

        res = requests.get(url)
        content = res.json()
        # print(content)

        items = content['data'][5:8]
        msgs_latest = f"1. {items[0]['word']}。2. {items[1]['word']}。3. {items[2]['word']}。"

        return msgs_latest
    except Exception as e:
        print(e)
        return "No interesting things yet."


if __name__ == '__main__':
    system_msg_updater = SystemMessageUpdater()

    print(system_msg_updater.latest_news)
    print(system_msg_updater.system_msg)

    system_message = system_msg_updater.get_system_message()
    # print(system_message)

    system_msg_updater.start(5.0)

    for _ in range(15):
        system_msg_updater.update()
        time.sleep(1.0)

    print("Over.")
