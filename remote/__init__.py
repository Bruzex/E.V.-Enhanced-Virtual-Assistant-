"""
E.V. (Enhanced Virtual Assistant) — Remote & Phone Bridge Subsystem
===================================================================
WhatsApp Web integration (Selenium), ADB over Wi-Fi phone controller,
and VB-Audio Virtual Cable live call routing.
"""

from remote.audio_routing import AudioRouter, AudioRoutingController
from remote.phone_adb import PhoneADBController
from remote.whatsapp import WhatsAppController

__all__ = [
    "AudioRouter",
    "AudioRoutingController",
    "PhoneADBController",
    "WhatsAppController",
]
