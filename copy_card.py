import datetime
from gpiozero import LED, Button
from time import sleep, time
import os
from glob import glob
from shutil import disk_usage
import shlex
import subprocess
import threading
import queue

#TODO: rather than using pattern, check for .picopydest in each drive in /media/pi/
destination_pattern = '/media/pi/OCOTERO*'
source_pattern = '/media/pi/D4*'

status_led = LED(26)
error_led = LED(21)
progress_led = LED(17)

go_button = Button(19,hold_time=2)
cancel_button = Button(2,hold_time=2)
eject_button = Button(13,hold_time=2)

rsync_process = None
rsync_outq = None
rsync_thread = None
out_dir = None

status='idle'
print(status)

class NotEnoughSpaceError(Exception):
    """raised when destination does not have enough space for source files"""
    pass


def output_parser(process):
    out=[]
    for line in iter(process.stdout.readline,b''):
        out.append(line.decode('utf-8'))
    return out

def output_reader(process,outq):
    for line in iter(process.stdout.readline,b''):
        outq.put(line.decode('utf-8'))

def get_free_space(disk,scale=2**30):
    return float(disk_usage(disk).free)/scale

def get_used_space(disk,scale=2**30):
    return float(disk_usage(disk).used)/scale
   
def blink_error(n,reps=2):
    """blink the error led to send a message"""
    for r in range(reps):
        for i in range(n):
            error_led.on()
            sleep(0.2)
            error_led.off()
            sleep(0.2)
        sleep(0.4) 

def blink_progress_led(outof10):
    progress_led.blink(0.1,0.15,outof10)
    sleep(3-0.25*outof10)


def get_src_drive():
    #a source drive does not have a .picopydest in root directory
    #must find exactly one. if zero returns None, if >1 blinks error
    drives=glob('/media/pi/*')
    src_drives = []
    for d in drives:
        if not os.path.exists(f'{d}/.picopydest'):
            src_drives.append(d)
    if len(src_drives)>1:
        print('ERR: found multiple source drives')
        blink_error(3,2)
        return None
    elif len(src_drives)<1:
        return None
    return src_drives[0]

def get_dest_drive():
    #a destination drive has .picopydest in root directory
    #must find exactly one. if zero returns None, if >1 blinks error
    drives=glob('/media/pi/*')
    dest_drives = []
    for d in drives:
        print(f'checking for {d}/.picopydest')
        if os.path.exists(f'{d}/.picopydest'):
            dest_drives.append(d)
    if len(dest_drives)>1:
        print('ERR: found multiple destination drives')
        blink_error(4,2)
        return None
    elif len(dest_drives)<1:
        return None
    return dest_drives[0]

def eject_drive(source=True):
    """eject the source drive (source=True) or dest drive (source=False)"""
    print('attempting to eject')
    
    drive = get_src_drive() if source else get_dest_drive()
    print(drive)

    if drive is None:
        print("ERR: no drive to eject")
    else:
        #try to eject the disk with system eject command
        cmd=f'eject {drive}'
        print(cmd)
        response=subprocess.Popen(shlex.split(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT)
        #response.communicate()
        [print(r) for r in output_parser(response)]

    sleep(1)

def prepare_copy():
    print('checking for source and dest drives')
    
    source=get_src_drive()
    if source is None:
        print('ERR: no source found')
        return 'idle',None,None

    dest = get_dest_drive()
    if dest is None:
        print('ERR: no destination found. Dest should contain .picopydest in root')
        return 'idle',None,None
    #dest=glob(destination_pattern)
    #if len(dest)<1:
    #    blink_error(3,2)
    #    print('ERR: no destination found')
    #    return 'idle',None,None
    #elif len(dest)>1:
    #    blink_error(4,2)
    #    print('ERR: multiple destinations found')
    #    return 'idle',None,None
    #else:
    #    dest=dest[0]
    #    print('dest: '+dest)
    #
    #source=glob(source_pattern)
    #if len(source)<1:
    #    blink_error(3,3)
    #    print('ERR: no source found')
    #    return 'idle',None,None
    #elif len(source)>1:
    #    blink_error(4,3)
    #    print('ERR: multiple sources found')
    #    return 'idle',None,None
    #else:
    #    source=source[0]
    #    print('\tsource: '+source)
 

    #ok, now we know we have 1 source and 1 destination
    #check that enough space on the dest for source
    src_size = get_used_space(source)
    dest_free = get_free_space(dest) 
    print(f"\tsrc size: {src_size} Gb")
    print(f"\tdest free: {dest_free} Gb")
    if src_size>dest_free:
        print('ERR: not enough space on dest for source')
        blink_error(5,2)#raise NotEnoughSpaceError

    return 'ready_to_copy',source,dest

def start_progress_monitor_thread(source,dest,rsync_thread):
    progress_q = queue.Queue()

    progress_monitor_thread = threading.Thread(target=monitor_progress,args=(source, dest, progress_q,rsync_thread))
    progress_monitor_thread.start()
    return progress_monitor_thread, progress_q

def monitor_progress(source,dest,progress_q,rsync_thread):
    src_size = get_used_space(source)
    dest_free = get_free_space(dest) 
    while rsync_thread.is_alive():
        sleep(6)
        copied_size = dest_free - get_free_space(dest) 
        progress_float = copied_size / src_size
        progress_q.put(progress_float)

def start_copy_thread(source,dest):

    print("copying")
    sleep(0.5)
    start_time=time()
    time_str=datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir=dest+"/"+os.path.basename(source)+"_"+time_str
    cmd = "rsync -rvh --log-file=./rsync.log --min-size=1k --progress --exclude .Trashes --exclude .fsevents* --exclude System* --exclude .Spotlight* "+source+" "+out_dir
    print(cmd)
    rsync_process = subprocess.Popen(shlex.split(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT)
    
    #start a thread to watch the rsync process and catch output
    rsync_outq = queue.Queue()

    rsync_thread = threading.Thread(target=output_reader,args=(rsync_process,rsync_outq))
    rsync_thread.start()
	

    #I think now we return the queue, thread, and process
    #we can read the queue and terminate the process from outside this function
    #print("copy completed")
    #print(f"time to complete: {int(start_time-time())} sec")
    return ('copying',rsync_process,rsync_outq,rsync_thread,out_dir)

def check_integrity_of_copy(source,dest,out_dir):
    print("checking integrity of copy")
    start_time=time()
    cmd = "rsync -rvn --size-only --stats --min-size=1k --exclude .Trashes --exclude .fsevents* --exclude System* --exclude .Spotlight* "+source+" "+out_dir 
    print(cmd)
    check_process=subprocess.Popen(shlex.split(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT)
    #check_process.communicate()
    return_values = [f for f in output_parser(check_process) if "Number of regular files transferred" in f]
    print(return_values)

    n_files_out_of_sync=int(return_values[0].split(' ')[-1])
    print(n_files_out_of_sync)
    return (n_files_out_of_sync==0)

def go_button_pressed(): 
    print('status: '+status)
    print('go button pressed')
    status_led.blink()
    if status=='idle':
        print('checking source dest drives')
        try:
            status,source,dest=prepare_copy()
            status_led.on() if status=='ready_to_copy' else status_led.off()
        except NotEnoughSpaceException:
            blink_error(5,2)
            status='idle'
        print(status)
    elif status=='ready_to_copy':
        print('starting copy')
        print('source: '+source)
        status,rsync_process,rsync_outq,rsync_thread=start_copy_thread(source,dest)
        print(status)

    else:
        print('unknown status')
        sleep(1)

def cancel_button_held():
    print('cancel button held')
    if rsync_process is None:
        return 'idle'

    rsync_process.terminate()
    try:
        rsync_process.wait(timeout=2)
        print('== subprocess rsync_process exited with rx=', rsync_process.returncode)
    except subprocess.TimeoutExpired:
        print('subprocess rsync_process did not terminate in time')

    sleep(1)
    return 'idle'


#go_button.when_pressed = go_button_pressed
#cancel_button.when_held = cancel_button_held
# the main loop only catches user input and sends work to threads

#TODO: allow unmounting drives
#TODO: (done?) handle rsync errors and report if copy is complete or not after rsync thread finishes (maybe status='completed' or status='incomplete transfer')
#TODO: send output to a log instead of stdout

prev_status=None
while True:

    sleep(0.2)

    #handle user input
    if cancel_button.is_held: 
        status = cancel_button_held()
        print(status)
        sleep(3)
    elif go_button.is_pressed and status=='idle':
        status,source,dest = prepare_copy()
        print(status)
        sleep(1)
    elif go_button.is_pressed and status=='finished':
        print('user aknowledged finished transfer')
        status='idle'
    elif go_button.is_held and status=='incomplete transfer':
        #requires user to HOLD go button to aknowledge an incomplete transfer
        print('user akcnowledged incomplete transfer')
        status='idle'
    elif go_button.is_pressed and status=='ready_to_copy':
        #start copy thread
        status,rsync_process,rsync_outq,rsync_thread,out_dir=start_copy_thread(source,dest)
        progress_monitor_thread,progress_q  = start_progress_monitor_thread(source,dest,rsync_thread)
        print(status)
    elif eject_button.is_pressed:
        #wait to see if this is a simple press or hold:
        eject_button.wait_for_release(2)
        if eject_button.is_held:
            #eject the destination drive
            print('ejecting destination')
            eject_drive(source=False)
        else: #short press
            #eject the source drive
            print('ejecting source')
            eject_drive(source=True)

        
    #end-of-copy routine    
    elif status=='copying' and not rsync_thread.is_alive():
        #we are done copying, or it failed
        print('rsync thread finished')
        
        #report finished or incomplete transfer
        complete_transfer = check_integrity_of_copy(source,dest,out_dir)
        if complete_transfer:
            print('transfer was complete. Press Go to acknowledge.')
            status='finished'
        else:
            print('ERR: transfer was not complete. Hold Go to acknowledge.')
            status='incomplete_transfer'
    
    #check if status changed during this iteration
    status_changed = (status!=prev_status)
    if status_changed:
        print(f'status changed to {status}')
    
    #update LEDs and output depending on status:
    if status_changed:
        # status LED
        if status=='copying':
            status_led.blink(0.25,0.25,n=None,background=True)
        elif status=='idle':
            status_led.blink(0.1,2.9,n=None,background=True)
        elif status=='ready_to_copy':
            status_led.blink(1,1,n=None,background=True)
        elif status=='finished':
            status_led.on()
        else:
            status_led.off()
        
        #error LED
        if status=='incomplete_transfer':
            error_led.on()
        else:
            error_led.off()

        #progress LED
        if status=='finished':
            progress_led.on()
        elif status !='copying':
            progress_led.off()

    if status=='copying':

        #read lines from rsync output
        try:
            line=rsync_outq.get(block=False)
            print(line)
        except queue.Empty:
            pass #print('no lines in queue')
        
        #update status LED using messages from progress_q
        try:
            progress_float = progress_q.get(block=False)
            progress_outof10 = int(progress_float*10)
            blink_progress_led(progress_outof10)
        except queue.Empty:
            pass #print('no lines in queue')

    prev_status = status
