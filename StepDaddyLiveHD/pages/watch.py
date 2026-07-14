import reflex as rx
from rxconfig import config
from StepDaddyLiveHD import backend
from StepDaddyLiveHD.components import navbar, MediaPlayer
from StepDaddyLiveHD.step_daddy import Channel

media_player = MediaPlayer.create


class WatchState(rx.State):
    is_loaded: bool = False

    @rx.var
    def channel(self) -> Channel | None:
        self.is_loaded = False
        channel = backend.get_channel(str(self.channel_id))
        self.is_loaded = True
        return channel

    @rx.var
    def url(self) -> str:
        return f"{config.api_url}/stream/{self.channel_id}.m3u8"


def uri_card() -> rx.Component:
    return rx.card(
        rx.hstack(
            rx.button(
                rx.text(WatchState.url),
                rx.icon("link-2", size=20),
                on_click=[
                    rx.set_clipboard(WatchState.url),
                    rx.toast("Copied to clipboard!"),
                ],
                size="1",
                variant="surface",
                radius="full",
                color_scheme="gray"
            ),
            rx.button(
                rx.text("VLC"),
                rx.icon("external-link", size=15),
                on_click=rx.redirect(f"vlc://{WatchState.url}", is_external=True),
                size="1",
                color_scheme="orange",
                variant="soft",
                high_contrast=True,
            ),
            rx.button(
                rx.text("MPV"),
                rx.icon("external-link", size=15),
                on_click=rx.redirect(f"mpv://{WatchState.url}", is_external=True),
                size="1",
                color_scheme="purple",
                variant="soft",
                high_contrast=True,
            ),
            rx.button(
                rx.text("Pot"),
                rx.icon("external-link", size=15),
                on_click=rx.redirect(f"potplayer://{WatchState.url}", is_external=True),
                size="1",
                color_scheme="yellow",
                variant="soft",
                high_contrast=True,
            ),
            # width="100%",
            wrap="wrap",
        ),
        margin_top="1rem",
    )


@rx.page("/watch/[channel_id]")
def watch() -> rx.Component:
    return rx.box(
        navbar(),
        rx.container(
            rx.cond(
                config.proxy_content,
                rx.fragment(),
                rx.card(
                    rx.hstack(
                        rx.icon(
                            "info",
                        ),
                        rx.text(
                            "Proxy content is disabled on this instance. Web Player won't work due to CORS.",
                        ),
                    ),
                    width="100%",
                    margin_bottom="1rem",
                    background_color=rx.color("accent", 7),
                ),
            ),
            rx.center(
                rx.card(
                    rx.cond(
                        WatchState.channel.name,
                        rx.hstack(
                            rx.box(
                                rx.hstack(
                                    rx.card(
                                        rx.image(
                                            src=WatchState.channel.logo,
                                            width="60px",
                                            height="60px",
                                            object_fit="contain",
                                        ),
                                        padding="0",
                                    ),
                                    rx.box(
                                        rx.heading(WatchState.channel.name, margin_bottom="0.3rem", padding_top="0.2rem"),
                                        rx.box(
                                            rx.hstack(
                                                rx.cond(
                                                    WatchState.channel.tags,
                                                    rx.foreach(
                                                        WatchState.channel.tags,
                                                        lambda tag: rx.badge(tag, variant="surface", color_scheme="gray")
                                                    ),
                                                ),
                                            ),
                                        ),
                                        overflow="hidden",
                                        text_overflow="ellipsis",
                                        white_space="nowrap",
                                    ),
                                ),
                            ),
                            rx.tablet_and_desktop(
                                rx.box(
                                    rx.vstack(
                                        rx.button(
                                            rx.text(
                                                WatchState.url,
                                                overflow="hidden",
                                                text_overflow="ellipsis",
                                                white_space="nowrap",
                                            ),
                                            rx.icon("link-2", size=20),
                                            on_click=[
                                                rx.set_clipboard(WatchState.url),
                                                rx.toast("Copied to clipboard!"),
                                            ],
                                            size="1",
                                            variant="surface",
                                            radius="full",
                                            color_scheme="gray"
                                        ),
                                        rx.hstack(
                                            rx.button(
                                                rx.text("VLC"),
                                                rx.icon("external-link", size=15),
                                                on_click=rx.redirect(f"vlc://{WatchState.url}", is_external=True),
                                                size="1",
                                                color_scheme="orange",
                                                variant="soft",
                                                high_contrast=True,
                                            ),
                                            rx.button(
                                                rx.text("MPV"),
                                                rx.icon("external-link", size=15),
                                                on_click=rx.redirect(f"mpv://{WatchState.url}", is_external=True),
                                                size="1",
                                                color_scheme="purple",
                                                variant="soft",
                                                high_contrast=True,
                                            ),
                                            rx.button(
                                                rx.text("Pot"),
                                                rx.icon("external-link", size=15),
                                                on_click=rx.redirect(f"potplayer://{WatchState.url}", is_external=True),
                                                size="1",
                                                color_scheme="yellow",
                                                variant="soft",
                                                high_contrast=True,
                                            ),
                                            justify="end",
                                            width="100%",
                                        ),
                                    ),
                                ),
                            ),
                            justify="between",
                            padding_bottom="0.5rem",
                        ),
                    ),
                    rx.box(
                        rx.cond(
                            WatchState.channel_id != "",
                            media_player(
                                title=WatchState.channel.name,
                                src=WatchState.url,
                            ),
                            rx.center(
                                rx.spinner(size="3"),
                            ),
                        ),
                        width="100%",
                    ),
                    padding_bottom="0.3rem",
                    width="720px",
                   #width="100%",
                ),
            ),
            rx.fragment(
                rx.mobile_only(
                    uri_card(),
                ),
                rx.cond(
                    WatchState.is_loaded & ~WatchState.channel.name,
                    rx.tablet_and_desktop(
                        uri_card(),
                    ),
                ),
            ),
            size="4",
            padding_top="10rem",
        ),
    )
