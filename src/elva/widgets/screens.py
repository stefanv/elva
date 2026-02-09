"""
[`Textual`](https://textual.textualize.io/) screens for ELVA apps.
"""

from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import Input, Static

from elva.widgets.awareness import AwarenessView
from elva.widgets.config import ConfigView


class Dashboard(Screen):
    """
    Screen for displaying session information.

    It features a [`ConfigView`][elva.widgets.config.ConfigView] widget for
    displaying the current configuration parameters as well as
    an [`AwarenessView`][elva.widgets.awareness.AwarenessView] widget
    showing the active clients in the current session.
    """

    def compose(self):
        """
        Hook adding child widgets.
        """
        yield ConfigView()
        yield AwarenessView()

    def key_escape(self):
        """
        Hook executed on pressing the `Esc` key.

        It dismisses the screen.
        """
        self.dismiss()


class InputScreen(ModalScreen):
    """
    A plain modal screen with a single input field.
    """

    def compose(self):
        """
        Hook adding child widgets.
        """
        yield Input(placeholder="Filename to save to")

    def on_input_submitted(self, event: Message):
        """
        Hook executed on an [`Input.Submitted`][textual.widgets.Input.Submitted] message.

        Arguments:
            event: the message containing the submitted value.
        """
        self.dismiss(event.value)

    def key_escape(self):
        """
        Hook executed on pressing the `Esc` key.

        It dismisses the screen.
        """
        self.dismiss()


class ErrorScreen(ModalScreen):
    """
    Modal screen displaying an exception message.
    """

    exc: str
    """The exception message to display."""

    def __init__(self, exc: str, *args: tuple, **kwargs: dict):
        """
        Arguments:
            exc: the exception message to display.
            args: positional arguments passed to [`ModalScreen`][textual.screen.ModalScreen]
            kwargs: keyword arguments passed to [`ModalScreen`][textual.screen.ModalScreen]
        """
        super().__init__(*args, **kwargs)
        self.exc = exc

    def compose(self):
        """
        Hook arranging child widgets.
        """
        yield Static("The following error occured and the app will close now:")
        yield Static(self.exc)
        yield Static("Press any key or click to continue.")

    def on_button_pressed(self):
        """
        Hook called on a button pressed event.

        It dismisses the screen.
        """
        self.dismiss(self.exc)

    def on_key(self):
        """
        Hook called on a pressed key.

        It dismisses the screen.
        """
        self.dismiss(self.exc)

    def on_mouse_up(self):
        """
        Hook called on a released mouse button.

        It dismisses the screen.
        """
        self.dismiss(self.exc)
