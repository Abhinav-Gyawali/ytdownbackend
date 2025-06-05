from yt_dlp import YoutubeDL

video_url = 'https://www.youtube.com/watch?v=1Rn0OooGRDI'

ydl_opts = {}
with YoutubeDL(ydl_opts) as ydl:
  info_dict = ydl.extract_info(video_url, download=False) # download=False to only get info
  if 'formats' in info_dict:
    print("Available formats:")
    for f in info_dict['formats']:
            format_id = f.get('format_id')
            ext = f.get('ext')
            resolution = f.get('resolution')
            note = f.get('format_note')
            print(f"  ID: {format_id}, Ext: {ext}, Resolution: {resolution}, Note: {note}")
  else:
    print("No formats found for this video.")