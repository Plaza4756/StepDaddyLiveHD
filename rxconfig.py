import reflex as rx
import os


proxy_content = os.environ.get("PROXY_CONTENT", "TRUE").upper() == "TRUE"
socks5 = os.environ.get("SOCKS5", "")
cdp_host = os.environ.get("CDP_HOST", "http://cloakbrowser:9222")
# NSFW channels stay hidden unless HIDE_NSFW is explicitly turned off.
hide_nsfw = os.environ.get("HIDE_NSFW", "TRUE").strip().upper() not in ("FALSE", "0", "NO", "OFF")

print(f"PROXY_CONTENT: {proxy_content}\nSOCKS5: {socks5}\nHIDE_NSFW: {hide_nsfw}")

config = rx.Config(
    app_name="StepDaddyLiveHD",
    proxy_content=proxy_content,
    cdp_host = cdp_host,
    telemetry_enabled=False,
    socks5=socks5,
    hide_nsfw=hide_nsfw,
    show_built_with_reflex=False,
    plugins=[
        rx.plugins.SitemapPlugin(),
        rx.plugins.TailwindV4Plugin(),
        rx.plugins.RadixThemesPlugin(theme=rx.theme(appearance="dark", accent_color="red")),
    ],
)
