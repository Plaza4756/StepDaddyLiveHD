# 🎉 Status
✅ Working fine. Report any issues.

> ⚠️ **Important:** Now the project provides pre-compiled docker images synced to latest commits. No more local image build required. Make sure you have latest commits for repo pulled. Note the changes in `docker-compose.yml` and `.env` files.
---

# StepDaddyLiveHD 🚀

A self-hosted IPTV proxy built with [Reflex](https://reflex.dev), enabling you to watch over 1,000 📺 TV channels and search for live events or sports matches ⚽🏀. Stream directly in your browser 🌐 or through any media player client 🎶. You can also download the entire playlist (`playlist.m3u8`) and integrate it with platforms like Jellyfin 🍇 or other IPTV media players.

---

## ✨ Features

- **📱 Stream Anywhere**: Watch TV channels on any device via the web or media players.
- **📄 Playlist Integration**: Download the `playlist.m3u8` and use it with Jellyfin or any IPTV client.
- **⚙️ Customizable Hosting**: Host the application locally or deploy it via Docker with various configuration options.
- **m3u8 Stream Extraction Caching**: Improves network latency from few seconds to sub 1 second. This is useful when running the container behind vpn e.g. gluetun

---

## 🐳 Docker Installation (Recommended)

> ⚠️ **Important:** If you plan to use this application across your local network (LAN), you must set `REFLEX_API_URL` to the **local IP address** of the device hosting the server in `.env`.

1. Make sure you have Docker and Docker Compose installed on your system.
2. Clone the repository and navigate into the project directory:
   ```bash
   git clone https://github.com/Plaza4756/StepDaddyLiveHD
   cd StepDaddyLiveHD
   ```
3. Edit `.env` file as per your requirements. e.g. set `REFLEX_API_URL=http://<local ip>:3535`
4. Run the following command to start the application:
   ```bash
   docker compose up -d
   ```
5. Access the front end on `http://<local ip>:3535` via browser

To locally build the container image for developement:
1. Navigate into the project directory
2. Run the following command to build the container and start the application::
   ```bash
   docker compose -f docker-compose-dev.yml up -d --build
   ```
---

## 🖥️ Local Installation

1. Install Python 🐍 (tested with version 3.13).
2. Clone the repository and navigate into the project directory:
   ```bash
   git clone https://github.com/Plaza4756/StepDaddyLiveHD
   cd StepDaddyLiveHD
   ```
3. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```
4. Install the dependencies:
   ```bash
   pip install -r requirements.txt
   ```
5. Initialize Reflex:
   ```bash
   reflex init
   ```
6. Run the application in production mode:
   ```bash
   reflex run --env prod
   ```

---

## ⚙️ Configuration

### Environment Variables

- **PORT**: Set a custom front end (web ui) port for the server.
- **REFLEX_API_URL**: Set the domain or IP where the server is reachable.
- **SOCKS5**: Proxy DLHD traffic through a SOCKS5 server if needed.
- **PROXY_CONTENT**: Proxy video content itself through your server (optional). Leave it as TRUE to avoid any CORS errors while fetching the stream.
- **REFLEX_BACKEND_PORT**: Custom backend port for the server. Useful when running behind vpn (e.g. gluetun) and there is a port conflict. Leave unchanged otherwise.

---

## 🗺️ Site Map

### Pages Overview:

- **🏠 Home**: Browse and search for TV channels.
- **📥 Playlist Download**: Download the `playlist.m3u8` file for integration with media players.

---

## 📸 Screenshots

**Home Page**
<img alt="Home Page" src="https://files.catbox.moe/qlqqs5.png">

**Watch Page**
<img alt="Watch Page" src="https://files.catbox.moe/974r9w.png">


---

## 📚 Kodi Addon

Check out the [dlhd-kodi](https://github.com/Plaza4756/dlhd-kodi) for Kodi addon to access the daddylive channels!
