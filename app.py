from flask import Flask, request, render_template, send_file
import os
import whisper
from moviepy.video.io.VideoFileClip import VideoFileClip
from moviepy.video.VideoClip import TextClip
from moviepy.video.compositing.CompositeVideoClip import CompositeVideoClip



import tempfile

app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'outputs'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

model = whisper.load_model("base")

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        file = request.files['video']
        filepath = os.path.join(UPLOAD_FOLDER, file.filename)
        file.save(filepath)

        # 音声を文字起こし（字幕抽出）
        result = model.transcribe(filepath)

        # 字幕テキストから動画に字幕合成
        clip = mp.VideoFileClip(filepath)
        txt_clips = []
        for seg in result["segments"]:
            txt = mp.TextClip(seg['text'], fontsize=24, color='white', bg_color='black')
            txt = txt.set_start(seg['start']).set_end(seg['end']).set_position(('center', 'bottom'))
            txt_clips.append(txt)

        final = mp.CompositeVideoClip([clip, *txt_clips])
        output_path = os.path.join(OUTPUT_FOLDER, 'subtitled_' + file.filename)
        final.write_videofile(output_path, codec='libx264', audio_codec='aac')

        return send_file(output_path, as_attachment=True)

    return render_template('index.html')

if __name__ == '__main__':
    app.run(debug=True)
import whisper

# 音声をテキストに変換する関数
def transcribe_audio(filepath):
    model = whisper.load_model("base")  # 軽くて速いモデルを使用
    result = model.transcribe(filepath)
    return result['text']
from flask import Flask, request, render_template
import os

app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

@app.route('/', methods=['GET', 'POST'])
def index():
    text = ''
    if request.method == 'POST':
        file = request.files['video']
        if file:
            filepath = os.path.join(UPLOAD_FOLDER, file.filename)
            file.save(filepath)

            # ここで文字起こし実行！
            text = transcribe_audio(filepath)

    return render_template('index.html', text=text)
import whisper

def transcribe_audio(filepath):
    model = whisper.load_model("base")
    result = model.transcribe(filepath)
    return result["text"]
