INSTALL:
yt-dlp
ffmpeg
python (id try python 3.11)

Although a public working invidious was used, you can create your own local invidious (recommended in case they're down)
- Steps to install local invidious: https://docs.invidious.io/installation/ 
  - You can run through windows docker but it's much better to use linux server because windows may not be stable (download WSL and Ubantu for linux server in windows)

The setup is through a local server, but it's the same as how you would set up tuberepair https://github.com/kevinf100/tuberepair.uptimetrackers.com. Instead of https://tuberepair.uptimetrackers.com/, use http://YOUR_LOCAL_IP:4000 in the tuberepair tweak.

Note:
- due to limitations on invidious, views for featured, most viewed, and related wont show. publishing date also wont show for featued and most viewed
- no login system cuz idk how. sorry. too complicated
- channel vids wont load cuz youtube keeps getting the handle or name instead of the channel ID due to a youtube handle update
- vids will take time to load, depending on video length, since it has to download and convert before playing which takes time, and the built-in yt unfortunately doesn't support direct streaming

You can send a pull request if you have one or you can provide feedback in https://docs.google.com/forms/d/e/1FAIpQLSf9kGghct4cjndq19QUXI64ODxCYKXFkJ_uBOkVjeek3FFRvQ/viewform?usp=publish-editor
