## THE PROJECT IS STILL IN DEVELOPEMENT. YOU MAY EXPECT BUGS AND ISSUES

# Setting it up

### INSTALL:
- yt-dlp
- ffmpeg
- python (id try python 3.11)

Although a public working invidious was used, you can create your own local invidious (recommended in case they're down)
- Steps to install local invidious: https://docs.invidious.io/installation/ 
  - You can run through windows docker but it's much better to use linux server because windows may not be stable (download WSL and Ubantu for linux server in windows)

The setup is through a local server, but it's the same as how you would set up tuberepair https://github.com/kevinf100/tuberepair.uptimetrackers.com. Instead of https://tuberepair.uptimetrackers.com/, use http://YOUR_LOCAL_IP:4000 in the tuberepair tweak.

Still confused on how to set it up? Here's a tutorial https://youtu.be/M25eJ0s6X18

### Notes
- be aware of any invidious sites not working anymore. If that happens, either find another working public invidious instance or use a local instance
  - And let me know if invidious site is down and you have another fully working instance I can update to my code
- this is NOT a replacement for tuberepair.uptimetrackers.com. It is again a self-hosted instance that fixed the playback error for iOS 3 devices especially and to fix the search function for accurate results + more coming up
  - ie. if you're using ios 4.2.1 on iphone 3g or ipod Touch 2G and outside without a public instance, use http://tuberepair.uptimetrackers.com
  - direct streaming does work for ios 4.2.1, so uptimetrackers url will load videos 
- due to limitations on invidious, views for featured, most viewed, and related wont show. Publishing date also wont show for featued and most viewed
- A proxy may be needed for the "more" tab to work for ios 3 users, at least in my case
- History (if you logged in) may not show for ios 3 users and idk about "my videos". Hopefully a fix soon
- Playlists, for some reason, crash on ios 3
- Pages like featured, most viewed, top rated, and most recent loads slow, but we are just experimenting things to make the best search results
- commenting, adding videos to playlists, and translating channel IDs to names dont work for now
- There is no real video description because you can't directly get the description from Invidious. Even if I did add, it makes it slow to load videos on the front page because they are separate. But it's not crazy slow (takes 20 seconds to load), but if people really want it, I guess I can add it.
- the channel names are all just channel IDs (just like Lincoln's tuberepair) because of an XML change that's required for channel uplaods to work
  - To compromise for it, it'll be added in the description (ex. Author name - Title of video)
- vids will take time to load, depending on video length, since it has to download and convert before playing which takes time, and the built-in yt unfortunately doesn't support direct streaming
- most viewed won't work because of invidious bug. Wait until that gets fixed ig
- The video load time will depend on your CPU and internet speed. Avoid playing 24 hour or 100 hour videos because that will take very very very long to load and may take up storage. 
- Youtube may be very slow if you have many videos downloading in the backend. If that happens, restart the server using Ctrl + C or restart your computer if your computer becomes slow. Also, remove any other videos in your static folder if they take up a lot of storage in the background or wait 10 minutes for them to be removed.

# Login feature
Big shoutout to [iOS 4 Xclusive](https://github.com/newfuckingplayer) for integrating the login system with the login from [Linocln's tuberepair server](https://github.com/erievs/Lincoln/)

### Logging in to youtube
_You must get your own GOOGLE CLIENT ID and GOOGLE CLIENT SECRET_
- Instructions: [How to Setup Login to your Tuberepair Server](https://drive.google.com/file/d/1M_Yd9HtXLN53N8o-W1liwkkHlHuLN0L0/view?usp=drive_link)
- Go to any page on youtube that will show a title like "Login here: <url_to_login>" and copy and paste the link in description to a modern device
- Follow the instructions from there

You can send a pull request if you have one or you can provide feedback in https://docs.google.com/forms/d/e/1FAIpQLSf9kGghct4cjndq19QUXI64ODxCYKXFkJ_uBOkVjeek3FFRvQ/viewform?usp=publish-editor or in the issues tab
