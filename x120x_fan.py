"""Pi 5 OS-managed fan curves. Boot-file writes require explicit helper approval."""
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import shutil
import struct
import tempfile
import time

BOOT_CONFIG = '/boot/firmware/config.txt'
BEGIN = '# BEGIN x120x_term-control-dash fan curve'
END = '# END x120x_term-control-dash fan curve'


@dataclass(frozen=True)
class FanCurve:
    temperatures: tuple = (50.0, 60.0, 67.5, 75.0)
    speeds: tuple = (30.0, 50.0, 70.0, 100.0)
    hysteresis: tuple = (5.0, 5.0, 5.0, 5.0)

    def validate(self):
        if any(len(values) != 4 for values in (self.temperatures,self.speeds,self.hysteresis)):
            raise ValueError('A fan curve requires four temperatures, speeds and hysteresis values')
        if any(not isinstance(value,(int,float)) or not math.isfinite(value)
               for values in (self.temperatures,self.speeds,self.hysteresis) for value in values):
            raise ValueError('Fan curve values must be finite numbers')
        if any(not 30 <= value <= 75 for value in self.temperatures):
            raise ValueError('Fan temperatures must be between 30C and 75C')
        temps = [round(value*1000) for value in self.temperatures]
        if any(a >= b for a,b in zip(temps,temps[1:])):
            raise ValueError('The four fan temperatures must increase in order')
        if any(not 20 <= value <= 100 for value in self.speeds):
            raise ValueError('Fan speeds must be between 20% and 100%')
        if any(a > b for a,b in zip(self.speeds,self.speeds[1:])) or self.speeds[-1] != 100:
            raise ValueError('Fan speeds must not decrease; the final stage must be 100%')
        if any(not 1 <= value <= 10 for value in self.hysteresis):
            raise ValueError('Temperature hysteresis must be between 1C and 10C')

    def pwm(self):
        return tuple(round(value*255/100) for value in self.speeds)

    def parameters(self):
        values = {'cooling_fan':'on'}
        for index in range(4):
            values[f'fan_temp{index}'] = str(round(self.temperatures[index]*1000))
            values[f'fan_temp{index}_speed'] = str(self.pwm()[index])
            values[f'fan_temp{index}_hyst'] = str(round(self.hysteresis[index]*1000))
        return values

    def summary(self):
        return [f'Stage {i+1}: {self.temperatures[i]:g}C -> {self.speeds[i]:g}% '
                f'(PWM {self.pwm()[i]}/255); hysteresis {self.hysteresis[i]:g}C' for i in range(4)]


def parse_values(text, repeat=False):
    values = tuple(float(item.strip()) for item in text.split(','))
    return values*4 if repeat and len(values)==1 else values


def proposed_curve(path=BOOT_CONFIG):
    try:
        values=saved_parameters(Path(path).read_text())
        if values:
            curve=FanCurve(tuple(int(values[f'fan_temp{i}'])/1000 for i in range(4)),
                tuple(round(int(values[f'fan_temp{i}_speed'])*100/255,1) for i in range(4)),
                tuple(int(values[f'fan_temp{i}_hyst'])/1000 for i in range(4)))
            curve.validate()
            return curve,'Starting from the saved project curve.'
    except (OSError,ValueError,KeyError):
        pass
    return FanCurve(),'Starter curve; these are proposed, not detected OS values.'


def block_span(text):
    # Refuse ambiguous/truncated edits instead of deleting unrelated boot content.
    lines=text.splitlines(keepends=True)
    starts=[i for i,line in enumerate(lines) if line.strip()==BEGIN]
    ends=[i for i,line in enumerate(lines) if line.strip()==END]
    if not starts and not ends:
        return lines,None
    if len(starts)!=1 or len(ends)!=1 or ends[0] <= starts[0]:
        raise ValueError('Fan configuration markers are incomplete or duplicated; review config.txt first')
    return lines,(starts[0],ends[0]+1)


def saved_parameters(text):
    lines,span=block_span(text)
    if span is None:
        return None
    parameters={}
    for line in lines[span[0]:span[1]]:
        match=re.fullmatch(r'\s*dtparam=(.*?)\s*',line.rstrip('\r\n'))
        if match:
            for item in match[1].split(','):
                key,sep,value=item.partition('=')
                if sep:
                    parameters[key]=value
    return parameters


def updated_boot_config(original,curve):
    curve.validate()
    if re.search(r'^\s*dtoverlay\s*=\s*(?:pwm-fan|pwm-gpio-fan|gpio-fan)\b',original,re.M):
        raise ValueError('A separate fan overlay is configured; this menu edits only the built-in Pi 5 fan')
    lines,span=block_span(original)
    if span:
        del lines[span[0]:span[1]]
    parameters=curve.parameters()
    # Enable the base cooling fan before closing overlay scope. This is a real
    # dtparam, so the subsequent empty dtoverlay cannot suppress HAT auto-loading.
    # Reset all conditional filters before selecting Pi 5, then reset scope again
    # at the end. Existing boot lines and overlays are otherwise kept verbatim.
    block=[BEGIN,'[all]','[pi5]','dtparam=cooling_fan=on','dtoverlay=','dtparam=cooling_fan=on']
    for i in range(4):
        block.append('dtparam='+','.join(f'{key}={parameters[key]}' for key in
                    (f'fan_temp{i}',f'fan_temp{i}_speed',f'fan_temp{i}_hyst')))
    block.extend(['[all]',END])
    remaining=''.join(lines)
    return remaining+('' if not remaining or remaining.endswith('\n') else '\n')+'\n'.join(block)+'\n'


def active_parameters(root=Path('/sys')):
    levels=root/'firmware/devicetree/base/cooling_fan/cooling-levels'
    try:
        data=levels.read_bytes()
        if len(data)!=20:
            return None
        pwm=struct.unpack('>5I',data)
        if pwm[0]!=0 or any(value>255 for value in pwm):
            return None
        trips=[]
        zone=root/'class/thermal/thermal_zone0'
        for type_file in zone.glob('trip_point_*_type'):
            if type_file.read_text().strip()!='active':
                continue
            prefix=str(type_file)[:-4]
            trips.append((int(Path(prefix+'temp').read_text()),int(Path(prefix+'hyst').read_text())))
        if len(trips)!=4:
            return None
        trips.sort()
        values={}
        for i,(temperature,hyst) in enumerate(trips):
            values.update({f'fan_temp{i}':str(temperature),f'fan_temp{i}_hyst':str(hyst),
                           f'fan_temp{i}_speed':str(pwm[i+1])})
        return values
    except (OSError,ValueError,struct.error):
        return None


def curve_status(path=BOOT_CONFIG,root=Path('/sys')):
    try:
        saved=saved_parameters(Path(path).read_text())
        if saved is None:
            return 'OS managed; no project curve saved'
        active=active_parameters(root)
        if active is None:
            return 'Saved curve; active curve unavailable (reboot if just saved)'
        keys=[f'fan_temp{i}{suffix}' for i in range(4) for suffix in ('','_hyst','_speed')]
        if all(saved.get(key)==active.get(key) for key in keys):
            return 'Saved curve is active'
        return 'Saved curve not active; reboot may be required'
    except (OSError,ValueError):
        return 'Fan boot settings unavailable'


def apply_curve(curve,ask=input,path=BOOT_CONFIG,model_path='/proc/device-tree/model'):
    curve.validate()
    model=Path(model_path).read_text().rstrip('\0\n')
    if model!='Raspberry Pi 5' and not model.startswith('Raspberry Pi 5 '):
        raise ValueError('Fan curve editing is supported only on Raspberry Pi 5')
    target=Path(path)
    original=target.read_text()
    updated=updated_boot_config(original,curve)
    if saved_parameters(original)==curve.parameters():
        print('This curve is already saved. Reboot if it has not taken effect.')
        return False
    print(f'Save Raspberry Pi OS fan settings to {target}:')
    print('\n'.join(curve.summary()))
    print('The built-in cooling fan will be enabled and remain temperature-controlled by the OS.\n'
          'The project requires full fan speed by 75C or below. CPU thermal protections remain in place.\n'
          'These settings override earlier fan parameters for Pi 5. Other boot settings are preserved.\n'
          'A backup is saved. A REBOOT IS REQUIRED; this command does not reboot or restart services.\n'
          'Charging, UPS shutdown and Telegram settings are not changed.')
    if ask('Confirmation 1 of 2 - save this fan curve? Type yes: ').strip().lower()!='yes':
        return False
    if ask('Confirmation 2 of 2 - type exactly "APPLY FAN CURVE": ').strip()!='APPLY FAN CURVE':
        return False
    write_boot_config(target,original,updated)
    return True


def write_boot_config(target,original,updated):
    if target.read_text()!=original:
        raise RuntimeError('Boot configuration changed during confirmation; nothing was saved. Retry.')
    backup=target.with_name(target.name+'.before-fan-'+str(time.time_ns()))
    shutil.copyfile(target,backup)
    fd,temporary=tempfile.mkstemp(prefix='.x120x-fan-',dir=target.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            # mkstemp defaults to 0600 on POSIX; boot files normally need 0644.
            # On FAT the mount determines modes; no permission change is required.
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temporary,target.stat().st_mode & 0o777)
        except PermissionError:
            pass
        os.replace(temporary,target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f'Fan settings saved. Backup: {backup}\nReboot when ready to apply them. No reboot was requested.')


def restore_os_control(ask=input,path=BOOT_CONFIG):
    target=Path(path)
    original=target.read_text()
    lines,span=block_span(original)
    if span is None:
        print('No project fan override is saved; the existing OS fan configuration is selected.')
        return False
    del lines[span[0]:span[1]]
    updated=''.join(lines)
    print('Remove the project fan curve and let the existing Raspberry Pi OS configuration choose the curve.\n'
          'Earlier manual OS settings are retained; this does not force factory defaults.\n'
          'A backup is saved. A REBOOT IS REQUIRED; no reboot or service restart is requested.\n'
          'Charging, UPS shutdown and Telegram settings are not changed.')
    if ask('Confirmation 1 of 2 - restore OS fan settings? Type yes: ').strip().lower()!='yes':
        return False
    if ask('Confirmation 2 of 2 - type exactly "RESTORE OS FAN CONTROL": ').strip()!='RESTORE OS FAN CONTROL':
        return False
    write_boot_config(target,original,updated)
    return True
