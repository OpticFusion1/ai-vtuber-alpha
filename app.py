# coding=utf8
import sys
import multiprocessing
import ctypes

import time
import numpy as np

import pyaudio

from torch import no_grad, LongTensor
from torch import device as torch_device

from revChatGPT.V3 import Chatbot as ChatbotV3

sys.path.append("vits")

import vits.utils as utils
import vits.commons as commons
from vits.models import SynthesizerTrn
from vits.text import text_to_sequence

import subtitle
import prompt_hot_update

from app_utils import *
import song_singer

from system_message_manager import SystemMessageManager

from vts_utils import ExpressionHelper, VTSAPIProcess, VTSAPITask

from Danmaku import DanmakuProcess

class VITSProcess(multiprocessing.Process):
    def __init__(
            self,
            device_str,
            task_queue,
            result_queue,
            event_initialized):
        multiprocessing.Process.__init__(self)
        self.device_str = device_str
        self.task_queue = task_queue  # VITS inference task queue
        self.result_queue = result_queue  # Audio data queue
        self.event_initialized = event_initialized

    def get_text(self, text, hps):
        text_norm, clean_text = text_to_sequence(text, hps.symbols, hps.data.text_cleaners)
        if hps.data.add_blank:
            text_norm = commons.intersperse(text_norm, 0)
        text_norm = LongTensor(text_norm)
        return text_norm, clean_text

    def vits(self, text, language, speaker_id, noise_scale, noise_scale_w, length_scale):
        if not len(text):
            return "Input text cannot be empty!", None, None
        text = text.replace('\n', ' ').replace('\r', '').replace(" ", "")
        # if len(text) > 100:
        #     return f"The input text is too long!{len(text)}>100", None, None
        if language == 0:
            text = f"[ZH]{text}[ZH]"
        elif language == 1:
            text = f"[JA]{text}[JA]"
        else:
            text = f"{text}"
        stn_tst, clean_text = self.get_text(text, self.hps_ms)

        start = time.perf_counter()
        with no_grad():
            x_tst = stn_tst.unsqueeze(0).to(self.device)
            x_tst_lengths = LongTensor([stn_tst.size(0)]).to(self.device)
            speaker_id = LongTensor([speaker_id]).to(self.device)
            audio = self.net_g_ms.infer(x_tst, x_tst_lengths, sid=speaker_id, noise_scale=noise_scale,
                                        noise_scale_w=noise_scale_w,
                                        length_scale=length_scale)[0][0, 0].data.cpu().float().numpy()
        print(f"The inference takes {time.perf_counter() - start} seconds")

        return audio

    def run(self):
        proc_name = self.name
        print(f"Initializing {proc_name}...")

        print(f"Using {self.device_str}")
        self.device = torch_device(self.device_str)

        self.hps_ms = utils.get_hparams_from_file(r'vits/model/config.json')
        speakers = self.hps_ms.speakers

        with no_grad():
            self.net_g_ms = SynthesizerTrn(
                len(self.hps_ms.symbols),
                self.hps_ms.data.filter_length // 2 + 1,
                self.hps_ms.train.segment_size // self.hps_ms.data.hop_length,
                n_speakers=self.hps_ms.data.n_speakers,
                **self.hps_ms.model).to(self.device)
            _ = self.net_g_ms.eval()
            model, optimizer, learning_rate, epochs = utils.load_checkpoint(r'vits/model/G_953000.pth', self.net_g_ms, None)

        print("Loading Weights finished.")

        self.event_initialized.set()

        while True:
            next_task = self.task_queue.get()
            if next_task is None:
                # Poison pill means shutdown
                print(f"{proc_name}: Exiting")
                break
            try:
                print(f"{proc_name} is working...")
                audio = None
                if next_task.text is not None:
                    audio = self.vits(next_task.text, next_task.language, next_task.sid, next_task.noise_scale,
                                    next_task.noise_scale_w, next_task.length_scale)

                # data = audio.astype(np.float32).tobytes()

                task = AudioTask(audio, 
                                next_task.text, 
                                pre_speaking_event=next_task.pre_speaking_event,
                                post_speaking_event=next_task.post_speaking_event)
                self.result_queue.put(task)

            except Exception as e:
                print(e)
                # print(f"Errors ocurrs in the process {proc_name}")

        sys.exit() # Manually and forcibly exit the process


class VITSTask:
    def __init__(self, text, language=0, speaker_id=2, noise_scale=0.5, 
                 noise_scale_w=0.5, length_scale=1.0, pre_speaking_event=None, post_speaking_event=None):
        self.text = text
        self.language = language
        self.sid = speaker_id
        self.noise_scale = noise_scale
        self.noise_scale_w = noise_scale_w
        self.length_scale = length_scale
        self.pre_speaking_event = pre_speaking_event
        self.post_speaking_event = post_speaking_event


# By ChatGPT
def normalize_audio(audio_data):
    # Calculate the maximum absolute value in the audio data
    max_value = np.max(np.abs(audio_data))
    
    # Normalize the audio data by dividing it by the maximum value
    normalized_data = audio_data / max_value
    
    return normalized_data


class AudioTask:
    def __init__(self, data, text=None, pre_speaking_event=None, post_speaking_event=None):
        self.data = data
        self.text = text
        self.pre_speaking_event = pre_speaking_event
        self.post_speaking_event = post_speaking_event


class AudioPlayerProcess(multiprocessing.Process):
    NUM_FRAMES = 1024
    BIT_DEPTH = 32
    NUM_BYTES_PER_SAMPLE = BIT_DEPTH // 8
    NUM_CHANNELS = 1
    CHUNK_SIZE = NUM_FRAMES * NUM_BYTES_PER_SAMPLE * NUM_CHANNELS # Data chunk size in bytes

    def __init__(self, audio_task_queue, subtitle_task_queue, sing_queue, vts_api_queue, event_initalized):
        super().__init__()
        self.task_queue = audio_task_queue
        self.subtitle_task_queue = subtitle_task_queue
        self.sing_queue = sing_queue
        self.vts_api_queue = vts_api_queue
        self.event_initalized = event_initalized

        self.enable_audio_stream = multiprocessing.Value(ctypes.c_bool, True)
        self.enable_audio_stream_virtual = multiprocessing.Value(ctypes.c_bool, True)

        self.virtual_audio_devices_are_found = False  # Maybe incorrect, because __init__ is run in the main process

    def set_audio_stream_enabled(self, value):
        self.enable_audio_stream.value = value

    def is_audio_stream_enabled(self):
        return self.enable_audio_stream.value

    def set_enable_audio_stream_virtual(self, value):
        self.enable_audio_stream_virtual.value = value

    def is_audio_stream_virtual_enabled(self):
        return self.enable_audio_stream_virtual.value

    def get_virtual_audio_indices(self):
        assert self.py_audio is not None

        self.virtual_audio_input_device_index = None
        self.virtual_audio_output_device_index = None

        # Search for valid virtual audio input and output devices
        for i in range(self.py_audio.get_device_count()):
            device_info = self.py_audio.get_device_info_by_index(i)
            if ("CABLE Output" in device_info['name'] and
                    device_info['hostApi'] == 0):
                assert device_info['index'] == i
                self.virtual_audio_input_device_index = i

            if ("CABLE Input" in device_info['name'] and
                    device_info['hostApi'] == 0):
                assert device_info['index'] == i
                self.virtual_audio_output_device_index = i

        if (self.virtual_audio_input_device_index is None or
                self.virtual_audio_output_device_index is None):
            print("Error: no valid virtual audio devices found!!!")
            self.virtual_audio_devices_are_found = False
        else:
            self.virtual_audio_devices_are_found = True

    # https://stackoverflow.com/questions/434287/how-to-iterate-over-a-list-in-chunks
    def chunker(self, seq, size):
        return [seq[pos:pos + size] for pos in range(0, len(seq), size)]

    def run(self):
        proc_name = self.name
        print(f"Initializing {proc_name}...")

        # https://people.csail.mit.edu/hubert/pyaudio/docs/
        # https://stackoverflow.com/questions/30675731/howto-stream-numpy-array-into-pyaudio-stream  
        self.py_audio = pyaudio.PyAudio()
        stream = self.py_audio.open(format=pyaudio.paFloat32,
                                    channels=1,
                                    rate=22050,
                                    output=True)

        self.get_virtual_audio_indices()

        stream_virtual = None
        if self.virtual_audio_devices_are_found:
            stream_virtual = self.py_audio.open(format=pyaudio.paFloat32,
                                                channels=1,
                                                rate=22050,
                                                output=True,
                                                output_device_index=self.virtual_audio_output_device_index)

        print("PYAudio is initialized.")

        self.event_initalized.set()

        while True:
            next_task = self.task_queue.get()
            if next_task is None:
                # Poison pill means shutdown
                print(f"{proc_name}: Exiting")
                break
            try:
                print(f"{proc_name} is working...")
                pre_speaking_event = next_task.pre_speaking_event
                if pre_speaking_event is not None:
                    if pre_speaking_event.event_type == SpeakingEvent.SING:
                        self.sing_queue.put(pre_speaking_event.msg)
                    elif pre_speaking_event.event_type == SpeakingEvent.SET_EXPRESSION:
                        data_dict = ExpressionHelper.create_expression_data_dict(pre_speaking_event.msg)
                        if data_dict != None:
                            msg_type = "ExpressionActivationRequest"

                            vts_api_task = VTSAPITask(msg_type, data_dict)
                            self.vts_api_queue.put(vts_api_task)
                    elif pre_speaking_event.event_type == SpeakingEvent.TRIGGER_HOTKEY:
                        msg_type = "HotkeyTriggerRequest"
                        data_dict = ExpressionHelper.create_hotkey_data_dict(pre_speaking_event.msg)

                        vts_api_task = VTSAPITask(msg_type, data_dict)
                        self.vts_api_queue.put(vts_api_task)

                audio = next_task.data
                text = next_task.text
                
                if text is not None:
                    self.subtitle_task_queue.put(text)
                
                if audio is not None:
                    audio = normalize_audio(audio)
                    data = audio.view(np.uint8)

                    # Write speech data into virtual audio device to drive lip sync animation
                    chunks = self.chunker(data, self.CHUNK_SIZE)
                    for chunk in chunks:
                        if self.is_audio_stream_enabled():
                            stream.write(chunk)

                        if (self.is_audio_stream_virtual_enabled() and
                                self.virtual_audio_devices_are_found):
                            stream_virtual.write(chunk)

                post_speaking_event = next_task.post_speaking_event
                if post_speaking_event is not None:
                    if post_speaking_event.event_type == SpeakingEvent.SING:
                        time.sleep(1.0) # a quick hack to delay continue to sing
                        self.sing_queue.put(post_speaking_event.msg)
                    elif post_speaking_event.event_type == SpeakingEvent.SET_EXPRESSION:
                        data_dict = ExpressionHelper.create_expression_data_dict(post_speaking_event.msg)
                        if data_dict != None:
                            msg_type = "ExpressionActivationRequest"

                            vts_api_task = VTSAPITask(msg_type, data_dict)
                            self.vts_api_queue.put(vts_api_task)
                    elif post_speaking_event.event_type == SpeakingEvent.TRIGGER_HOTKEY:
                        msg_type = "HotkeyTriggerRequest"
                        data_dict = ExpressionHelper.create_hotkey_data_dict(post_speaking_event.msg)

                        vts_api_task = VTSAPITask(msg_type, data_dict)
                        self.vts_api_queue.put(vts_api_task)

            except Exception as e:
                print(e)

        stream.close()
        stream_virtual.close()
        self.py_audio.terminate()
        

def should_cut_text(text, min, punctuations_min, threshold, punctuations_threshold):
    should_cut = False
    if len(text) >= min:
        if text[-1] in punctuations_min:
            should_cut = True
        elif len(text) >= threshold:
            if text[-1] in punctuations_threshold:
                should_cut = True

    return should_cut


class ChatGPTProcess(multiprocessing.Process):
    def __init__(self, api_key, greeting_queue, chat_queue, thanks_queue, cmd_queue, vits_task_queue,
                 app_state, event_initialized):
        super().__init__()
        self.api_key = api_key

        self.greeting_queue = greeting_queue
        self.chat_queue = chat_queue
        self.thanks_queue = thanks_queue
        self.cmd_queue = cmd_queue

        self.vits_task_queue = vits_task_queue
        self.event_initialized = event_initialized

        self.use_streamed = multiprocessing.Value(ctypes.c_bool, False)
        self.enable_vits = multiprocessing.Value(ctypes.c_bool, True)
        
        self.app_state = app_state

    def set_vits_enabled(self, value):
        self.enable_vits.value = value

    def is_vits_enabled(self):
        return self.enable_vits.value

    def set_streamed_enabled(self, value):
        self.use_streamed.value = value

    def is_streamed_enabled(self):
        return self.use_streamed.value

    def run(self):
        proc_name = self.name
        print(f"Initializing {proc_name}...")

        # system_msg_updater = prompt_hot_update.SystemMessageUpdater()
        # system_msg_updater.start(60.0)

        system_message_manager = SystemMessageManager()

        song_list = song_singer.SongList()

        # engine_str = "gpt-3.5-turbo-0301"
        # chatbot = ChatbotV3(api_key=self.api_key, engine=engine_str, temperature=0.7, system_prompt=preset_text)
        chatbot = ChatbotV3(api_key=self.api_key, max_tokens=3000, temperature=0.7,
                            system_prompt=preset_text_short)
        
        chatbot.timeout = 30.0

        punctuations_to_split_text = {'。', '！', '？', '：', '\n'}
        punctuations_to_split_text_longer = {'。', '！', '？', '：', '\n', '，'}

        min_sentence_length = 12
        sentence_longer_threshold = 24

        # Use short preset text for event
        chatbot.reset(convo_id='default', system_prompt=preset_text_short)

        channels = {'default', 'chat', 'presing'}

        curr_song_dict = None

        self.event_initialized.set()

        print(f"{proc_name} is Initialized.")

        while True:
            # system_msg_updater.update()

            if self.app_state.value == AppState.CHAT:
                task = None
                if not self.thanks_queue.empty():
                    print(f"{proc_name} is working...")
                    print("Get a task from thanks_queue.")
                    task = self.thanks_queue.get()
                elif not self.chat_queue.empty():
                    print(f"{proc_name} is working...")
                    print("Get a task from chat_queue.")
                    task = self.chat_queue.get()
                    if task is None:
                        # Poison pill means shutdown
                        print(f"{proc_name}: Exiting")
                        break

                    header = task.message[:32]
                    if '#reset' in header:
                        try:
                            print("Reset ChatGPT")
                            print(preset_text_short)
                            chatbot.conversation.clear()
                            chatbot.reset(convo_id='default', system_prompt=preset_text_short)
                        except Exception as e:
                            print(e)
                            print("Reset fail!")
                        finally:
                            continue

                    if task.message.startswith("Song request"):
                        cmd_sing_str = task.message 
                        song_alias = cmd_sing_str[2:]

                        user_name = task.user_name
                        song_dict = song_list.search_song(song_alias)
                        channel = 'presing'

                        curr_song_dict = song_dict

                        editor_name = None
                        song_name = None
                        song_abbr = None
                        id = None
                        if song_dict is not None:
                            song_name = song_dict['name']
                            song_abbr = song_dict['abbr']
                            editor_name = song_dict['editor']
                            id = song_dict['id']

                        if editor_name is not None and editor_name == '_':
                            editor_name = None

                        # Maybe the code should behave like 
                        # if Song ID doesn't exist, then the character should tell the audience that the Song ID does not exist
                        if id is not None and id == 666:
                            system_msg = SystemMessageManager_1.get_presing_special_sm(user_name)
                        else:
                            system_msg = SystemMessageManager_1.get_presing_sm(user_name, song_abbr, editor_name)

                        chatbot.reset(convo_id=channel, system_prompt=system_msg)

                        cmd_sing = None
                        if song_dict is not None:
                            cmd_sing = f"Song request{song_name}"

                        try:
                            new_sentence = ""
                            vits_task = None
                            for data in chatbot.ask_stream(prompt="", convo_id=channel):
                                print(data, end="|", flush=True)

                                if vits_task is not None:
                                    self.vits_task_queue.put(vits_task)
                                    vits_task = None

                                new_sentence += data
                                should_cut = should_cut_text(new_sentence, 
                                                        min_sentence_length, 
                                                        punctuations_to_split_text, 
                                                        sentence_longer_threshold,
                                                        punctuations_to_split_text_longer)

                                # If code reaches here, meaning that the request to ChatGPT is successful.
                                if self.is_vits_enabled():
                                    if should_cut:
                                        vits_task = VITSTask(new_sentence.strip())
                                        new_sentence = ""

                            if self.is_vits_enabled():
                                if vits_task is None:
                                    vits_task = VITSTask(new_sentence.strip())
                                else:
                                    assert len(new_sentence) == 0

                                if song_dict is not None:
                                    vits_task.post_speaking_event = SpeakingEvent(SpeakingEvent.SING, cmd_sing)

                                self.vits_task_queue.put(vits_task)

                        except Exception as e:
                            print(e)
                            if id != 666:
                                if song_dict is not None:
                                    if editor_name is not None:
                                        response_to_song_request_msg = f"OK, “{user_name}” Classmates, I will sing a song for you now{song_abbr}，grateful {editor_name} The boss taught me to sing this song."
                                    else:
                                        response_to_song_request_msg = f"OK, “{user_name}” Classmates, I will sing a song for you now{song_abbr}。"
                                else:
                                    response_to_song_request_msg = f"sorry, {user_name} Classmate, I can’t sing the song you ordered."
                            else:
                                response_to_song_request_msg = f"“{user_name}” Classmate, I am really angry now!"
                            
                            vits_task = VITSTask(response_to_song_request_msg)
                            
                            if song_dict is not None:
                                vits_task.post_speaking_event = SpeakingEvent(SpeakingEvent.SING, cmd_sing)

                            self.vits_task_queue.put(vits_task)

                        if song_dict is not None:
                            self.app_state.value = AppState.PRESING
                        continue

                # elif not self.greeting_queue.empty():
                #     print(f"{proc_name} is working...")
                #     print("Get a task from greeting_queue.")
                #     task = self.greeting_queue.get()

                else:
                    time.sleep(1.0)
                    continue

                assert task is not None
                
                user_name = task.user_name
                msg = task.message
                channel = task.channel
                prompt_msg = msg

                assert channel in channels

                try:
                    repeat_user_message = True
                    repeat_message = None
                    c_id = None
                    if channel == 'default':
                        c_id = channel
                        repeat_user_message = False
                        if channel in chatbot.conversation:
                            if len(chatbot.conversation[c_id]) >= 9:
                                chatbot.reset(convo_id=c_id)
                    elif channel == 'chat':
                        c_id = user_name
                        if (channel not in chatbot.conversation or 
                            len(chatbot.conversation[c_id]) >= 9):
                            # system_msg = system_msg_updater.get_system_message()
                            system_msg = system_message_manager.systetm_message
                            chatbot.reset(c_id, system_msg)

                        repeat_message = f"{user_name} explain：“{msg}”"
                        # prompt_msg = f"（{user_name} say to you：)“{msg}”"
                        prompt_msg = f"My screen name is “{user_name}”，{msg}"

                    new_sentence = ""
                    is_first_sentence = True
                    for data in chatbot.ask_stream(prompt=prompt_msg, convo_id=c_id):
                        print(data, end='|', flush=True)
                        
                        new_sentence += data
                        should_cut = should_cut_text(new_sentence, 
                                                min_sentence_length, 
                                                punctuations_to_split_text, 
                                                sentence_longer_threshold,
                                                punctuations_to_split_text_longer)

                        # If code reaches here, meaning that the request to ChatGPT is successful.
                        if self.is_vits_enabled():
                            if should_cut:
                                if repeat_user_message:
                                    vits_task = VITSTask(repeat_message.strip())
                                    vits_task.pre_speaking_event = SpeakingEvent(SpeakingEvent.TRIGGER_HOTKEY, "MoveEars")
                                    self.vits_task_queue.put(vits_task)
                                    # time.sleep(1.0) # Simulate speech pause
                                    repeat_user_message = False

                                if is_first_sentence:
                                    emotion, line = ExpressionHelper.get_emotion_and_line(new_sentence)
                                    print(f"#{line}#")
                                    print(f"emotion: {emotion}")
                                    vits_task = VITSTask(line.strip())
                                    if emotion in ExpressionHelper.emotion_to_expression:
                                        vits_task.pre_speaking_event = SpeakingEvent(SpeakingEvent.SET_EXPRESSION, emotion)
                                    is_first_sentence = False
                                else:
                                    vits_task = VITSTask(new_sentence.strip())

                                self.vits_task_queue.put(vits_task)
                                new_sentence = ""

                    if len(new_sentence) > 0:
                        if self.is_vits_enabled():
                            pre_speaking_event = None
                            line = new_sentence
                            if is_first_sentence:
                                emotion, line = ExpressionHelper.get_emotion_and_line(new_sentence)
                                if emotion in ExpressionHelper.emotion_to_expression:
                                    pre_speaking_event = SpeakingEvent(SpeakingEvent.SET_EXPRESSION, emotion)
                                is_first_sentence = False
                            
                            vits_task = VITSTask(line.strip(), 
                                                pre_speaking_event=pre_speaking_event)
                            self.vits_task_queue.put(vits_task)

                    if self.is_vits_enabled():
                        # A task with no text for TTS, just for SpeakingEvent at the end of the conversation.
                        end_task = VITSTask(None,
                                            post_speaking_event=SpeakingEvent(SpeakingEvent.TRIGGER_HOTKEY, "Clear"))
                        self.vits_task_queue.put(end_task)

                except Exception as e:
                    print(e)
                    if channel == 'chat':
                        if self.is_vits_enabled():
                            # text = "Sorry, I was distracted just now. What did you just say?"
                            text = "Oyster~ I just broke my bone again!"
                            task = VITSTask(text)
                            self.vits_task_queue.put(task)

            elif self.app_state.value == AppState.PRESING:
                # Clear all queues
                while not self.chat_queue.empty():
                    _ = self.chat_queue.get()

                # while not self.greeting_queue.empty():
                    # _ = self.greeting_queue.get()

                while not self.thanks_queue.empty():
                    _ = self.thanks_queue.get()

                cmd_msg = self.cmd_queue.get()
                if cmd_msg == "#Singing begins":
                    self.app_state.value = AppState.SING
                elif cmd_msg == "#End of singing":
                    self.app_state.value = AppState.CHAT
                if cmd_msg is None:
                    # Poison pill means shutdown
                    print(f"{proc_name}: Exiting")
                    break

            elif self.app_state.value == AppState.SING:
                if not self.cmd_queue.empty():
                    cmd_msg = self.cmd_queue.get()

                    if cmd_msg == "#End of singing":

                        song_abbr = curr_song_dict['abbr']
                        editor_name = curr_song_dict['editor']
                        id = curr_song_dict['id']
                        channel = "postsing"

                        if editor_name == '_':
                            editor_name = None

                        if id == 666:
                            system_msg = SystemMessageManager_1.get_finish_special_sm()
                        else:
                            system_msg = SystemMessageManager_1.get_finish_sm(song_abbr, editor_name)
                        chatbot.reset(convo_id=channel, system_prompt=system_msg)
                        
                        try:
                            new_sentence = ""
                            for data in chatbot.ask_stream(prompt="", convo_id=channel):
                                print(data, end="|", flush=True)

                                new_sentence += data
                                should_cut = should_cut_text(new_sentence, 
                                                    min_sentence_length, 
                                                    punctuations_to_split_text, 
                                                    sentence_longer_threshold,
                                                    punctuations_to_split_text_longer)

                                if should_cut:
                                    vits_task = VITSTask(new_sentence.strip())
                                    self.vits_task_queue.put(vits_task)
                                    new_sentence = ""

                            if len(new_sentence) > 0:
                                if self.is_vits_enabled():
                                    vits_task = VITSTask(new_sentence.strip())
                                    self.vits_task_queue.put(vits_task)
                        except Exception as e:
                            print(e)
                            if id != 666:
                                if editor_name is not None:
                                    post_sing_line = f"Thank you friends for enjoying this song {song_abbr}，Thanks again {editor_name} Boss, he taught me how to sing!"
                                else:
                                    post_sing_line = f"Thank you friends for enjoying this song {song_abbr}！"
                            else:
                                post_sing_line = "Please don't anger me again next time!"

                            vits_task = VITSTask(post_sing_line)
                            self.vits_task_queue.put(vits_task)
                        finally:
                            self.app_state.value = AppState.CHAT
                    elif cmd_msg == "ONE CLICK THREE CONNECT":
                        # test_interrupted_line = "Is there anything you want to say to me? It's okay, I'll keep singing~"
                        test_interrupted_line = "If you like my singing, please remember to click three times in a row~"
                        pre_event = SpeakingEvent(SpeakingEvent.SING, "#interrupt singing")
                        post_event = SpeakingEvent(SpeakingEvent.SING, "#keep singing")
                        vits_task = VITSTask(test_interrupted_line, 
                                            pre_speaking_event=pre_event, 
                                            post_speaking_event=post_event)
                        self.vits_task_queue.put(vits_task)

                    elif cmd_msg is None:
                        # Poison pill means shutdown
                        print(f"{proc_name}: Exiting")
                        break

                elif not self.thanks_queue.empty():
                    task = self.thanks_queue.get()

                    user_name = task.user_name
                    msg = task.message
                    channel = "sing"

                    system_msg = SystemMessageManager_1.sing_thanks
                    chatbot.reset(convo_id=channel, system_prompt=system_msg)

                    try:
                        first_sentence = True
                        new_sentence = ""
                        vits_task = None
                        for data in chatbot.ask_stream(prompt=msg, convo_id=channel):
                            print(data, end='|', flush=True)

                            if vits_task is not None:
                                if first_sentence:      
                                    vits_task.pre_speaking_event = SpeakingEvent(SpeakingEvent.SING, "#打断唱歌")                        
                                    self.vits_task_queue.put(vits_task)
                                    vits_task = None   
                                    first_sentence = False

                            new_sentence += data

                            should_cut = should_cut_text(new_sentence, 
                                                        min_sentence_length, 
                                                        punctuations_to_split_text, 
                                                        sentence_longer_threshold,
                                                        punctuations_to_split_text_longer)
                            
                            # If code reaches here, meaning that the request to ChatGPT is successful.
                            if self.is_vits_enabled():
                                if should_cut:
                                    vits_task = VITSTask(new_sentence.strip())
                                    new_sentence = ""

                        if self.is_vits_enabled():
                            if vits_task is not None:
                                assert len(new_sentence) == 0
                                vits_task.post_speaking_event = SpeakingEvent(SpeakingEvent.SING, "#keep singing")
                            else:
                                vits_task = VITSTask(new_sentence.strip(), 
                                                    post_speaking_event=SpeakingEvent(SpeakingEvent.SING, "#keep singing"))
                                
                            if first_sentence:
                                vits_task.pre_speaking_event = SpeakingEvent(SpeakingEvent.SING, "#interrupt singing")

                            self.vits_task_queue.put(vits_task)

                    except Exception as e:
                        print(e)
                        response_to_interuption_msg = f"Thank you very much for your recognition! Do you want to sing along with me?"
                        if self.is_vits_enabled():
                            vits_task = VITSTask(response_to_interuption_msg, 
                                                pre_speaking_event=SpeakingEvent(SpeakingEvent.SING, "#interrupt singing"),
                                                post_speaking_event=SpeakingEvent(SpeakingEvent.SING, "#keep singing")
                                                )

                            self.vits_task_queue.put(vits_task)

                else:
                    time.sleep(1.0)
                    
class SpeakingEvent:
    SING = 0
    SET_EXPRESSION = 1
    TRIGGER_HOTKEY = 2

    def __init__(self, event_type, msg):
        self.event_type = event_type
        self.msg = msg


preset_text_short = "Now you are given an identity. You are a horse racing girl named Donghai Emperor. You are chatting with the audience live in the live broadcast room of Station B. You often refer to yourself as "young master". You speak concisely. "

class SystemMessageManager_1:
    sing_accept_thank_tempate = "Now you are given an identity. You are a horse racing girl named Emperor of the East China Sea, and you are preparing to sing on the victory stage. You often refer to yourself as "young master". An audience member in the audience, named "{t_user_name}", ordered a song called "{t_song_name}". This song was taught to you by the boss "{t_editor_name}". You agreed to sing it and thanked you for teaching. Your boss, now you say to this audience: (All your speech must follow the following rules: 1. You must speak in the first person. 2. The content of your speech should not be described in the third person. 3. The content of your speech Do not bring any action description.）"

    sing_accept_tempate = "Now you are given an identity. You are a horse racing girl named Emperor of the East China Sea, and you are preparing to sing on the victory stage. You often refer to yourself as "young master". An audience member in the audience, named "{t_user_name}", ordered a song named "{t_song_name}". You can sing it and agree to sing it. At this time, you say to this audience member: (All your words must be Follow the following rules: 1. Speech must be in the first person. 2. The content of the speech should not be described in the third person. 3. The content of the speech should not be described in any action.) "

    # You can randomly recommend songs, there is a list
    sing_refuse_template = "Now you are given an identity. You are a horse racing girl named Emperor of the East China Sea, and you are preparing to sing on the victory stage. You often refer to yourself as "young master". An audience member in the audience, named "{t_user_name}", ordered a song, but you can't sing it. At this time, you said to this audience member: (All your speech must follow the following rules: 1. It must be in the first person. Speak. 2. Don’t use third-person descriptions in your speech. 3. Don’t include any action descriptions in your speech.) "

    sing_finish_template = "Now you are given an identity. You are a horse racing girl, and your name is Donghai Emperor. You often call yourself "young master". You just finished singing a song on the victory stage. The name of this song is "{t_song_name}" and it was taught to you by the boss "{t_editor_name}". At this time, you want to thank the audience for listening, and thank the guy who taught you this song again, you say: (All your words must follow the following rules: 1. Must speak in the first person. 2. Speak The content should not be described in the third person. 3. The content of the speech should not be described in any action.) "

    sing_finish_no_editor_template = "Now you are given an identity. You are a horse racing girl, and your name is Donghai Emperor. You often call yourself "young master". You just finished singing a song on the victory stage. The name of the song is "{t_song_name}". At this time, you want to thank the audience for listening, and you say: (All your speech must follow the following rules: 1. You must speak in the first person. 2. The content of your speech should not be described in the third person. 3. Speak Do not include any action descriptions in the content.) "

    sing_thanks = "Now you are given an identity. You are a horse racing girl named Donghai Emperor, and you are singing live on the victory stage. You often refer to yourself as "young master". If an audience sends you a gift, likes, or follows your live broadcast while you are singing, you will pause your singing to thank the audience. All your speech must comply with the following rules: 1. It must be spoken in the first person. 2. Do not describe the content in the third person. 3. Don’t include any action description in your speech. "

    speical_accept_template = "Now you are given an identity. You are a horse racing girl named Emperor of the East China Sea, and you are preparing to sing on the victory stage. You often refer to yourself as "young master". An audience member in the audience, named "{t_user_name}", tried to irritate you. You said to this audience member: (All your speech content must comply with the following rules: 1. You must speak in the first person. 2. Do not include any content in your speech. The content is described in the third person. 3. Do not include any action description in the speech content.) "

    speical_finish_template = "Now you are given an identity. You are a horse racing girl named Emperor of the East China Sea, and you are singing on the victory stage. You often refer to yourself as "young master". You were very angry just now and hoped that the audience would stop irritating you. You said to the audience: (All your speech content must follow the following rules: 1. You must speak in the first person. 2. Do not use the third person to describe the content of your speech. Content. 3. Do not include any description of actions in the content of your speech.) "

    def get_presing_sm(user_name, song_name=None, editor_name=None):
        if song_name is not None:
            if editor_name is not None:
                return SystemMessageManager_1.sing_accept_thank_tempate.format(t_user_name=user_name, 
                                                                       t_song_name=song_name,
                                                                       t_editor_name=editor_name)
            else:
                return SystemMessageManager_1.sing_accept_tempate.format(t_user_name=user_name, t_song_name=song_name)
        else:
            return SystemMessageManager_1.sing_refuse_template.format(t_user_name=user_name)
        
    def get_finish_sm(song_name, editor_name=None):
        if editor_name is not None:
            return SystemMessageManager_1.sing_finish_template.format(t_song_name=song_name, t_editor_name=editor_name)
        else:
            return SystemMessageManager_1.sing_finish_no_editor_template.format(t_song_name=song_name)
    
    def get_presing_special_sm(user_name):
        return SystemMessageManager_1.speical_accept_template.format(t_user_name=user_name)
    
    def get_finish_special_sm():
        return SystemMessageManager_1.speical_finish_template

if __name__ == '__main__':
    app_state = multiprocessing.Value('i', AppState.CHAT)

    room_id = ""

    greeting_queue = multiprocessing.Queue(maxsize=2)
    chat_queue = multiprocessing.Queue(maxsize=3)
    thanks_queue = multiprocessing.Queue(maxsize=4)

    event_danmaku_process_stop = multiprocessing.Event()

    damaku_process = DanmakuProcess(room_id, greeting_queue, chat_queue, thanks_queue, app_state, event_danmaku_process_stop)
    damaku_process.start()

    cmd_queue = multiprocessing.Queue(maxsize=4)
    sing_queue = multiprocessing.Queue(maxsize=4)

    event_song_singer_process_initialized = multiprocessing.Event()
    song_singer_process = song_singer.SongSingerProcess(sing_queue, cmd_queue, event_song_singer_process_initialized)
    song_singer_process.start()

    vits_task_queue = multiprocessing.Queue()

    event_chat_gpt_process_initialized = multiprocessing.Event()

    api_key = ""
    chat_gpt_process = ChatGPTProcess(api_key, greeting_queue, chat_queue, thanks_queue, cmd_queue, vits_task_queue,
                                      app_state, event_chat_gpt_process_initialized)
    chat_gpt_process.start()

    audio_task_queue = multiprocessing.Queue()

    event_vits_process_initialized = multiprocessing.Event()

    # Or cpu
    device_str = 'cuda'
    # device_str = 'cpu'
    vits_process = VITSProcess(
        device_str,
        vits_task_queue,
        audio_task_queue,
        event_vits_process_initialized)
    vits_process.start()

    event_audio_player_process_initialized = multiprocessing.Event()
    subtitle_task_queue = multiprocessing.Queue()

    vts_api_queue = multiprocessing.Queue(maxsize=8)
    audio_player_process = AudioPlayerProcess(audio_task_queue, subtitle_task_queue, sing_queue, vts_api_queue,
                                              event_audio_player_process_initialized)
    audio_player_process.start()

    event_subtitle_bar_process_initialized = multiprocessing.Event()

    subtitle_bar_process = subtitle.SubtitleBarProcess(subtitle_task_queue, event_subtitle_bar_process_initialized)
    subtitle_bar_process.start()

    vts_api_process = VTSAPIProcess(vts_api_queue)
    vts_api_process.start()

    event_subtitle_bar_process_initialized.wait()

    event_song_singer_process_initialized.wait()
    event_chat_gpt_process_initialized.wait()
    event_vits_process_initialized.wait()
    event_audio_player_process_initialized.wait()

    while True:
        user_input = input("Please enter commands:\n")
        if user_input == 'esc':
            break
        if app_state.value == AppState.CHAT:
            if user_input == '0':
                if chat_gpt_process.is_vits_enabled():
                    chat_gpt_process.set_vits_enabled(False)
                    print("Disable VITS")
                else:
                    chat_gpt_process.set_vits_enabled(True)
                    print("Enable VITS")
            elif user_input == '1':
                if audio_player_process.is_audio_stream_enabled():
                    audio_player_process.set_audio_stream_enabled(False)
                    print("Disable Audio stream")
                else:
                    audio_player_process.set_audio_stream_enabled(True)
                    print("Enable Audio stream")
            elif user_input == '2':
                if audio_player_process.is_audio_stream_virtual_enabled():
                    audio_player_process.set_enable_audio_stream_virtual(False)
                    print("Disable virtual audio stream")
                else:
                    audio_player_process.set_enable_audio_stream_virtual(True)
                    print("Enable virtual audio stream")
            elif user_input == '3':
                if damaku_process.is_response_enabled():
                    damaku_process.set_response_enabled(False)
                    print("Disable response to live comments")
                else:
                    damaku_process.set_response_enabled(True)
                    print("Enable response to live comments")
            elif user_input == '5':
                if chat_gpt_process.is_streamed_enabled():
                    chat_gpt_process.set_streamed_enabled(False)
                    print("Disable chatGPT streamed")
                else:
                    chat_gpt_process.set_streamed_enabled(True)
                    print("Enable ChatGPT streamed")

            elif user_input == '8':
                print("Reset ChatGPT")
                chat_task = ChatTask(None, "#reset", 'chat')
                chat_queue.put(chat_task)
            elif user_input == '9':
                print("Test VITS and audio player")
                test_text = "Test speech synthesis and audio playback."
                vits_task_queue.put(VITSTask(test_text))
            else:
                chat_task = ChatTask('Meow meow convulsions', user_input, 'chat')
                chat_queue.put(chat_task)
        elif app_state.value != AppState.CHAT:
            if user_input == "#End of singing":
                sing_queue.put("#cut song")
            elif user_input == "#One click three consecutive":
                cmd_queue.put("#One click three consecutive")
            elif user_input == "#like":
                msg = f"Thumbs up for you!"
                task = ChatTask(None, msg, 'default')
                thanks_queue.put(task)

    event_danmaku_process_stop.set()
    # Clear all queues
    clear_queue(chat_queue)
    chat_queue.put(None)
    print("The chat_queue is cleared")
    clear_queue(vits_task_queue)
    vits_task_queue.put(None)
    print("The vits_task_queue is cleared")
    clear_queue(audio_task_queue)
    audio_task_queue.put(None)
    print("The audio_task_queue is cleared")
    clear_queue(subtitle_task_queue)
    subtitle_task_queue.put(None)
    print("The subtitle_task_queue is cleared")
    clear_queue(sing_queue)
    sing_queue.put(None)
    print("The sing_queue is cleared")
    clear_queue(cmd_queue)
    cmd_queue.put(None)
    print("The cmd_queue is cleared")
    clear_queue(vts_api_queue)
    vts_api_queue.put(None)
    print("The vts_api_queue is cleared")

    print("All queues are cleared")

    chat_gpt_process.join()
    print("chat_gpt_process is joined")
    damaku_process.join()
    print("damaku_process is joined")
    song_singer_process.join()
    print("song_singer_process is joined")
    audio_player_process.join()
    print("audio_player_process is joined")
    subtitle_bar_process.join()
    print("subtitle_bar_process is joined")
    vts_api_process.join()
    print("vts_api_process is joined")
    vits_process.join()
    print("vits_process is joined")
