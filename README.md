# About

This is a small KVM client for old supermicro boards which have an ATEN BMC. These boards do not support HTML KVM. To connect to the BMc, a java tool is to be used. The java tool comes along with platform specific libraries (.so for linux and dll for windows). Those libraries are compiled for x86 platform. consequently someone can not run the KVM client on other hardware platfurms such as a rasperry pi.

More problems are

- the supermicro tool is changing window size if framebuffer resolution changes. if you use an i3 desktop, reboot the server and switch to another virtual desktop (for ssh), the java window will follow you when the framebuffer resolution ich chaning
- no proper resize
- no bandwidth control. it is hard to switch the tool to a bandwidth saving mode. no statistics how much bandwidth is used (yes there are people who pay for traffic!)
- no shortcuts for important keystroke macros such as switching virtual terminals
- no paste functionality - yes, sometimes you have very complex root passwords you need to apply quickly. sometimes you might also want to copy snippets to your kvm
- client terminates if network connection drops

# motivation

My servers are based in Germany. When I lived in Germany the supermicro KVM viewer was no fun but it was bearable. Living near to the servers bandwith opitmization was not a grat issue. client termination afer rarely connection drops were tolerable.

currently i live in sunnyvale, ca and operating my German servers through KVM become a mess. As you only use KVM for sweaty jobs (such as rebuilding a raid or checking settings in the bios) you can imagine that i do liked to continue with the orignal tool.

so it was time to make a custom one. 

# what you get

this tool is implementing the proprietary supermicro kvm wire protocol. there are several tools out there but none of them worked flawlessly for me on my X10 boards. This implementation works. As i intended to have a small lean tool i did not tried to enhance existing tools but if you like, feel free to use this code as a template. 

The current version implements:

- a platform independent implementation of the aten KVM protocol as it is used on old supermicro boards (tested X10, but some newer and older models will also work for sure) 
- a gui which does not change its window size when there are events on your server (resolution change, power-off, reboot etc). 
- in i3 desktop environment gui sticks to the selected i3 virtual desktop and stays there
- toolbar with shortcuts
    - cad macro (ctrl+alt+del) for reboots
    - magic systems request key macro (linux kernel reisub for safe hard reboots)
    - for fast bios interaction: F11, F12, del macros
    - VT menue with macros to switch between virtual consoles 
    - scr up, scr down (macros for screen program to page up/down shell output)
    - snap - make a screenshot and put it to local clipboard
    - paste - provides a dialog where text could entered and pasted to the kvm session. slow so that bmc is not crashing (yes it does sometimes)
- connection lost detection and reconnect (handy if you work with vpn tunnels)
- mapping between your local keyboard mapping and remote mapping (sometimes keyboards on target are different)
- bandwidth control - shows statistics bandwidth wich is currently used and counts transferred bytes
- quality control - it is possible to reduce the bandwith which is necessary for operation to a minimum (unfortunately even if all savings are utilized those old devices are still consuming more than modern protocols would do)
- descriptive debug messages
- you can have a config file holding all your kvm data and use a menue on start of the tool selecting the desired kvm
- mouse input can be turned off - if you just work on remote server text console, there is no need to send mouse info


# danger zone

do never operate these older kvm units directly connected to the internet! They just do crypto on the keyboard/mouse but that crypto is not effective at all. The screen frames are plain! therefore my client has NOT implemented any crypto at all - all keystrokes are sent plain. Use a VPN to connect securely to your KVM! An old raspbery pi or some other small board would do the job.

# how it was made

my ideas proudly brought to life by AI ... see the other .md files how things are working.

