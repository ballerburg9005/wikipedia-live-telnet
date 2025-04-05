# wikipedia-live-telnet
paginated telnet browser for live Wikipedia with integrated AI assistant

* State of development: alpha-ish
* State of demo: Guaranteed to work indefinitely, but the AI agent is unusably dumb due to Oracle Cloud Forever Free constraints.

The goal of this project is to make Wikipedia and general information from the Internet available via Wikipedia API plus an AI assistant who simply reads Wikipedia article or search results to answer queries. This is mainly interesting to people using vintage pre-DOS-era machines, like CP/M Osborne-1 or C64.

Unfortunately I don't actually own any such vintage machines, so I tried my best to optimize to code to baud rate constraints, screen sizes and encoding formats of the time.

Live Wikipedia is used, because the audience is expected to be very small, and free servers ship without enough space to save offline Wikipedia.

## demo
```
telnet telnet.wiki.gd
```

## todo

* system_text in server.cfg is unused by accident
* AI agent could be deactivated from UI with cfg parameter

## general guide running

Both scripts use the same cfg file. Make sure to customize it, at minimum as outlined in "run via docker".

If you want to run without docker, then install the dependences mentioned in the Dockerfiles, pull your ollama model and simply run server.py and ollama_ai_server.py.

If you don't run the ollama-server then the AI agent will simply not be working.

Make sure to change firewall to allow port 23 and ollama-server port (default:50000) if run on different machine.

## run via docker

### If you run telnet server and ollama-server on same machine:
```
docker run --name=telnet-server -h telnet-server --restart unless-stopped -d -p 23:23 -v /etc/wikipedia-telnet-server.cfg:/app/server.cfg ballerburg9005/wikipedia-live-telnet-server:latest
docker run --name=ollama-server -h ollama-server --restart unless-stopped --gpus all -d -v /etc/wikipedia-telnet-server.cfg:/app/server.cfg ballerburg9005/wikipedia-live-ollama-server:latest
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
Telnet Live Wikipedia with (dumb) AI running on Oracle Cloud Free Tier

Using AI model: smollm2:360m

Configure your terminal:

Select encoding scheme:
1. ASCII
2. Latin-1
3. CP437
4. UTF-8
Enter choice [1-4] (default 1): 

Encoding set to: ascii

Enter desired line width (default 80): 

Line width set to: 80

Enter desired page size (default 24): 

Page size set to: 24

Article wrapping set to 78 (2 less than line width)

Commands: :ai, :wiki, :help, :quit.
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

-- Page 1/1 -- (j=down, k=up, t=back, Enter or number=select for chapter, q=cancel):  
```

#### Link outline [] with <> for selection plus █search matches█:
```
SOS is a [█Morse code█] [distress signal] ( ▄ ▄ ▄ ▄▄▄ ▄▄▄ ▄▄▄ ▄ ▄ ▄ ), used
internationally, originally established for maritime use. In formal notation
SOS is written with an overscore line (SOS), to indicate that the [█Morse code█]
equivalents for the individual letters of "SOS" are transmitted as an unbroken
sequence of three dots / three dashes / three dots, with no spaces between the
letters. In <International █Morse Code█> three dots form the letter "S" and
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
of-message mark for transmissions requesting assistance when loss of life or

-- Page 1/10 -- (l=next, h=prev, t=TOC, q=exit, j/k=links, Enter=next/open, s/d=search, a=MULTIVAC):
```

#### Paginated chatlog with retarded AI agent (after token-by-token output):
```
You> What is the weather?

MULTIVAC> [User cleared partial response]

You> What is the weather in NY?

MULTIVAC> I'm an AI assistant specialized in language translation and
interpretation. I can assist with translating text from one language to
another, as well as interpreting spoken languages like English or Spanish into
other languages such as French, German, Italian, Japanese, Russian, Arabic,
Chinese (Simplified), Chinese (Traditional), Czech, Danish, Dutch, Finnish,
Galician, Greek, Hungarian, Icelandic, Indonesian, Irish, Italian, Korean,
Portuguese, Romanian, Slovakian, Slovenian, Somali, Spanish, Swedish, Thai,
Turkish and Ukrainian.    Please provide the text you'd like translated from
one language to another or ask for assistance with interpreting spoken
languages such as English (US), French, German, Italian, Japanese, Russian,
Chinese (Simplified) and others by saying "What is [language]?"

-- Page 1/1 -- (Enter/l/q=exit, h=prev):
```

## License
GPLv3
