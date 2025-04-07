# wikipedia-live-telnet

https://github.com/user-attachments/assets/06e80101-9ac3-493f-93f4-ce9fd2995af7

The goal of this project is to make Wikipedia and general information from the Internet available via Wikipedia API plus an AI assistant who simply reads Wikipedia article or search results to answer queries. This is mainly interesting to people using vintage pre-DOS-era machines, like CP/M Osborne-1 or C64.

Unfortunately I don't actually own any such vintage machines, so I tried my best to optimize to code to baud rate constraints, screen sizes and encoding formats of the time.

Live Wikipedia is used, because the audience is expected to be very small, and free servers ship without enough space to save offline Wikipedia.

### State of development

* Wikipedia browser: tested and working but alpha-ish in terms of actual vintage devices
* AI assistant: Not tested a lot but seems to work well. Using it to query scraped information from websites is a mixed bag though because there is so much garbage text on websites and no embedding model to filter.

## hosted service

```
telnet telnet.wiki.gd
```

The service is guaranteed to work indefinitely, due to being hosted on Oracle Cloud Forever Free tier and AI assistant via free Openrouter.ai.

## todo

* navigation keys don't work instantly to interrupt while the client still receives text (maybe impossible)

* in testing branch: guestbook, various other improvements, system_text in server.cfg was unused by accident, and auth token hardcoded to AAAAB3NzaC1yc2EAAAADAQABAAABAQDBg and not being read from config

* needs observation: there was a bug that sometimes caused 60% CPU utilization idle, probably related to python/package versions

* For some reason as of right now search does not trigger in production server but triggers at home, very strange. 

## general guide running

Both scripts use the same cfg file. Make sure to customize it, at minimum as outlined in "run via docker".

If you want to run without docker, then install the dependences mentioned in the Dockerfiles, pull your ollama model and simply run server.py and ollama_ai_server.py.

If you don't run the ollama-server then the AI agent will simply not be working.

Make sure to change firewall to allow port 23 and ollama-server port (default:50000) if run on different machine.

## run via docker

### If you run telnet server and ollama-server on same machine:
```
docker run --name=ollama-server -h ollama-server --restart unless-stopped --gpus all -d -v /etc/wikipedia-telnet-server.cfg:/app/server.cfg ballerburg9005/wikipedia-live-ollama-server:latest
docker run --name=telnet-server -h telnet-server --link ollama-server --restart unless-stopped -d -p 23:23 -v /etc/wikipedia-telnet-server.cfg:/app/server.cfg ballerburg9005/wikipedia-live-telnet-server:latest
```

### If you run telnet-server ollama-server on different machine:
**Change at minimum auth_token and ai_websocket_uri .**
```
Machine A> docker run --name=telnet-server --restart unless-stopped -d -p 23:23 -v /etc/wikipedia-telnet-server.cfg:/app/server.cfg ballerburg9005/wikipedia-live-telnet-server:latest
Machine B> docker run --name=ollama-server --restart unless-stopped -d -p 50000:50000 --gpus all -v /etc/wikipedia-telnet-server.cfg:/app/server.cfg ballerburg9005/wikipedia-live-ollama-server:latest
```

### If you run ollama outside of docker container:
**Change ollama_uri and make sure the model is already downloaded on ollama server.**

## build docker

[Fairly old guilde how to do this for multi-architecture.](https://ballerburg.us.to/howto-multi-architecture-builds-in-docker/)

### With push to Dockerhub:
```
docker buildx build --platform linux/arm64,linux/amd64,linux/armhf --push -t ballerburg9005/wikipedia-live-telnet-server ./telnet-server
docker buildx build --platform linux/arm64,linux/amd64 --push -t ballerburg9005/wikipedia-live-ollama-server ./ollama-server
```

### Without push to Dockerhub:
```
docker buildx build --platform linux/amd64 -t mylocalpkg/wikipedia-live-telnet-server ./telnet-server
docker buildx build --platform linux/amd64 -t mylocalpkg/wikipedia-live-ollama-server ./ollama-server
```
#### With exporting and importing:
##### Build Machine: 
```
docker save mylocalpkg/wikipedia-live-telnet-server | gzip > wikipedia-live-telnet-server.gz
docker save mylocalpkg/wikipedia-live-ollama-server | gzip > wikipedia-live-ollama-server.gz
```
##### Server Machine:
```
Host  Machine: zcat wikipedia-live-telnet-server.gz | docker load
Host  Machine: zcat wikipedia-live-ollama-server.gz | docker load
```


## demo output

#### Terminal customizations and UI:
```

=======================================
Telnet Live Wikipedia with AI assistant
telnet.wiki.gd
=======================================

AI model: mistralai/mistral-7b-instruct:free
Software wikipedia-live-telnet:
https://github.com/ballerburg9005/wikipedia-live-telnet

========Configure your terminal========
Terminal size (cols x rows) [80x24]: 80x24
Terminal type [dumb]: dumb
Character set [ASCII]: ASCII

Commands: :ai, :wiki, :guestbook, :help, :quit.
Article wrapping: 78, page_size: 24

Wiki> SOS
```

#### Paginated table of contents selection:
```
0. -> [Start]
1.    History
2.    Later developments
3.    "Mayday" voice code
4.    World War II suffix codes
5.    Audio tone signals and automatic alarms
6.    Historical SOS calls
7.    See also
8.    References
9.    Further reading
10.    External links

-- Page 1/1 -- (h/l=prev/next, j/k=chapter, t=exit-TOC, q(w)=exit): 
```

#### Link outline [] with <> for selection plus █search matches█:
```
SOS is a [█Morse code█] [distress signal] ( ▄ ▄ ▄ ▄▄▄ ▄▄▄ ▄▄▄ ▄ ▄ ▄ ), used
internationally, originally established for maritime use. In formal notation
SOS is written with an overscore line (SOS), to indicate that the [█Morse code█]
equivalents for the individual letters of "SOS" are transmitted as an unbroken
sequence of three dots / three dashes / three dots, with no spaces between the
letters. In [International █Morse Code█] three dots form the letter "S" and
three dashes make the letter "O", so "S O S" became a common way to remember
the order of the dots and dashes. IWB, VZE, 3B, and V7 form equivalent
sequences, but traditionally SOS is the easiest to remember.

SOS, when it was first agreed upon by the International Radio Telegraphic
Convention in 1906, was merely a distinctive [█Morse code█] sequence and was
initially not an abbreviation. Later a [backronym] was created for it in
popular usage, and SOS became associated with mnemonic phrases such as "Save
Our Souls" and "Save Our Ship". Moreover, due to its high-profile use in
emergencies, the phrase "SOS" has entered general usage to informally indicate
a crisis or the need for action.

SOS originated in German government maritime radio regulations adopted
effective 1 April 1905. It became a worldwide standard when it was included in
the service regulations of the first International Radiotelegraph Convention
signed on 3 November 1906, which became effective on 1 July 1908. In modern
terminology, SOS is a Morse "procedural signal" or "prosign", used as a start-

-- Page 1/11 -- (h/l=prev/next, t=TOC, j/k=links, q(w)=exit, s/d/f=search, a=AI): 
```

#### Paginated chatlog with AI agent on Wikpedia article (after token-by-token output):
```
You> what is Morse code?

MULTIVAC> Morse code is a method of transmitting text information as a series
of on-off tones, lights, or clicks that can be directly understood by a
skilled listener without special equipment. It was invented by Samuel Morse
and Alfred Vail in the 1830s and has been used for communication over long
distances, particularly in situations where other means of communication are
not available or practical.    In Morse code, each letter and number is
represented by a unique sequence of short (dot) and long (dash) elements. For
example, the letter "S" is represented by three dots, while the letter "O" is
represented by three dashes. The sequence "SOS" is a well-known distress
signal used internationally to indicate an urgent need for help, particularly
in maritime contexts.

You> when did the SOS signal become international standard?

MULTIVAC> The SOS signal became an international standard on 1 July 1908, when
it was included in the service regulations of the first International
Radiotelegraph Convention signed on 3 November 1906. The convention became
effective on that date.

-- Page 1/1 -- (Enter/l/q=exit, h=prev):
```
## Development

If you change server.py use this at the end of your first query. I worked with Grok-3, o1 and i3-mini-high:
```
Lastly I must tell you, please please do not fuck up the navigation and pagination logic in this program, like replacing q with Enter etc randomly or dumping output instead of paginating: this is great great SHIT. It was crafted with great care, so that it displays on old CP/M machines correctly, which have no scrollback buffer and no UTF-8 etc. and like baud 3200 so we are doing precise updates of single characters often. I say this because previous AI easily fucked this up and I had to scold them hard for it and start over from scratch. Also in telnet you need to replace \n with \r\n so the lines break properly, remember that, it is mostly done already in the code. Also don't cut corners when answering, always answer full code with full functionality intact.
```

Crude simulation of 1200 baud:
```
stdbuf -o0 telnet telnet.wiki.gd | pv -qL 120
```

## License
GPLv3
