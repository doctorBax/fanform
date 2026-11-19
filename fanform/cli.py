"""Terminal control panel: arrow keys, Enter, digits. Run as root."""
from __future__ import annotations
import argparse
import curses
import getpass
import hashlib
import os
import secrets
import shutil
import socket
import subprocess
import sys
from .core import CONFIG, load_json, save_json, emergency_restore, uninstall_restore

SERVICE = 'fanform.service'
TIMER = 'fanform-update.timer'


def cmd(*args, check=False):
    return subprocess.run(args, text=True, capture_output=True, check=check)


def status():
    active = cmd('systemctl','is-active',SERVICE).stdout.strip()
    updates = cmd('systemctl','is-enabled',TIMER).stdout.strip()
    config = load_json(CONFIG,{})
    return f'Сервис: {active}\nАдрес: {config.get("bind", "0.0.0.0")}:{config.get("port", 8796)}\nАвтообновления: {updates}'


def port_change(value):
    port = int(value)
    if not 1024 <= port <= 65535:
        raise ValueError('Порт должен быть 1024–65535')
    data = load_json(CONFIG,{})
    if port == data.get('port'): return 'Порт не изменился'
    bind = data.get('bind','0.0.0.0')
    sock = socket.socket()
    try:
        sock.bind((bind,port))
    finally:
        sock.close()
    data['port'] = port
    save_json(CONFIG,data)
    cmd('systemctl','restart',SERVICE,check=True)
    return f'Порт изменён: {port}'


def repo_ready():
    path = '/opt/fanform'
    return os.path.isdir(path+'/.git') and cmd('git','-C',path,'remote','get-url','origin').returncode==0


def auto_updates(turn_on):
    if turn_on and not repo_ready():
        raise ValueError('Для автообновлений нужна установка из GitHub git-репозитория')
    args = ('systemctl','enable','--now',TIMER) if turn_on else ('systemctl','disable','--now',TIMER)
    cmd(*args,check=True)
    return 'Автообновления включены' if turn_on else 'Автообновления выключены'


def reset_password():
    password=secrets.token_urlsafe(20)
    salt=secrets.token_hex(16)
    digest=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),250000).hex()
    data=load_json(CONFIG,{})
    data['password']={'salt':salt,'hash':digest}
    save_json(CONFIG,data)
    cmd('systemctl','restart',SERVICE,check=True)
    return password


def uninstall():
    cmd('systemctl','disable','--now',TIMER)
    cmd('systemctl','disable','--now',SERVICE)
    uninstall_restore()
    for f in ('/etc/systemd/system/fanform.service', '/etc/systemd/system/fanform-update.service',
              '/etc/systemd/system/fanform-update.timer','/usr/local/bin/fanformctl'):
        try: os.unlink(f)
        except FileNotFoundError: pass
    cmd('systemctl','daemon-reload',check=True)
    shutil.rmtree('/opt/fanform',ignore_errors=True)
    shutil.rmtree('/etc/fanform',ignore_errors=True)
    shutil.rmtree('/var/lib/fanform',ignore_errors=True)
    return 'Fanform и его конфигурация удалены'


def _prompt(stdscr,prompt):
    h,w=stdscr.getmaxyx()
    curses.echo(); curses.curs_set(1)
    try:
        stdscr.move(min(h-2,17),2)
        stdscr.clrtoeol()
        stdscr.addnstr(min(h-2,17),2,prompt,w-4)
        return stdscr.getstr(min(h-2,18),2,20).decode().strip()
    finally:
        curses.noecho(); curses.curs_set(0)


def menu(stdscr):
    curses.curs_set(0)
    stdscr.keypad(True)
    choices=['Статус сервиса','Изменить порт веб-интерфейса','Включить автообновления',
             'Выключить автообновления','Сменить пароль','Аварийное восстановление оборотов',
             'Удалить Fanform','Выход']
    current=0; message=''
    while True:
        stdscr.erase();h,w=stdscr.getmaxyx()
        try:
            stdscr.addnstr(1,2,'fanform / CONTROL TERMINAL',max(w-4,1),curses.A_BOLD)
            stdscr.addnstr(3,2,'↑ ↓  выбор     ENTER  действие     1–8  быстрый выбор',max(w-4,1))
            for i,item in enumerate(choices):
                stdscr.addnstr(i+5,3,f'{i+1}. {item}',max(w-6,1),curses.A_REVERSE if i==current else 0)
            stdscr.addnstr(15,2,message.replace('\n',' · '),max(w-4,1))
        except curses.error: pass
        stdscr.refresh()
        key=stdscr.getch()
        if key in (curses.KEY_DOWN,ord('j')): current=(current+1)%len(choices);continue
        if key in (curses.KEY_UP,ord('k')): current=(current-1)%len(choices);continue
        if ord('1')<=key<=ord('8'): current=key-ord('1')
        elif key not in (10,13,curses.KEY_ENTER):
            if key in (27,ord('q')): return
            continue
        try:
            if current==0: message=status()
            elif current==1:
                v=_prompt(stdscr,'Новый TCP-порт:')
                message=port_change(v) if v else 'Отменено'
            elif current==2: message=auto_updates(True)
            elif current==3: message=auto_updates(False)
            elif current==4:
                if _prompt(stdscr,'Ввести YES для сброса пароля:')=='YES':
                    message='Новый пароль: '+reset_password()
                else: message='Отменено'
            elif current==5:
                if _prompt(stdscr,'Ввести YES для экстренного сброса:')=='YES':
                    cmd('systemctl','stop',SERVICE)
                    emergency_restore();cmd('systemctl','start',SERVICE)
                    message='Питание вентиляторов восстановлено'
                else: message='Отменено'
            elif current==6:
                if _prompt(stdscr,'Ввести DELETE для удаления всего:')=='DELETE':
                    message=uninstall();stdscr.erase();stdscr.addstr(2,2,message);stdscr.refresh();stdscr.getch();return
                message='Отменено'
            elif current==7:return
        except Exception as e: message='Ошибка: '+str(e)


def main():
    if os.geteuid()!=0:
        sys.exit('Запустите через sudo fanformctl')
    p=argparse.ArgumentParser()
    p.add_argument('--port',type=int)
    p.add_argument('--status',action='store_true')
    p.add_argument('--auto-update',choices=['on','off'])
    p.add_argument('--reset-password',action='store_true')
    p.add_argument('--uninstall',action='store_true')
    p.add_argument('--emergency',action='store_true')
    args=p.parse_args()
    if args.emergency: emergency_restore();return
    if args.port: print(port_change(args.port));return
    if args.status: print(status());return
    if args.auto_update: print(auto_updates(args.auto_update=='on'));return
    if args.reset_password: print('Новый пароль:',reset_password());return
    if args.uninstall: print(uninstall());return
    curses.wrapper(menu)

if __name__=='__main__':main()
