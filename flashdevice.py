# pylint: disable=invalid-name
# pylint: disable=line-too-long
from array import array as Array
import time
import struct
import sys
import traceback
from pyftdi import ftdi
import ecc
import flashdevice_defs


def _as_bytes(data):
    if isinstance(data, bytes):
        return data
    if isinstance(data, bytearray):
        return bytes(data)
    if isinstance(data, str):
        return data.encode('latin1')
    return bytes(data)


def _resolve_mcu_bitmode(ftdi_obj):
    legacy_mode = getattr(ftdi_obj, 'BITMODE_MCU', None)
    if legacy_mode is not None:
        return legacy_mode

    bitmode_enum = getattr(ftdi.Ftdi, 'BitMode', None)
    if bitmode_enum is not None and hasattr(bitmode_enum, 'MCU'):
        return bitmode_enum.MCU

    return 0x08


class IO:
    def __init__(self, do_slow = False, debug = 0, simulation_mode = False):
        self.Debug = debug
        self.PageSize = 0
        self.OOBSize = 0
        self.PageCount = 0
        self.BlockCount = 0
        self.PagePerBlock = 0
        self.BitsPerCell = 0
        self.WriteProtect = True
        self.CheckBadBlock = True
        self.BadBlockMarkerOffset = 0
        self.RemoveOOB = False
        self.UseSequentialMode = False
        self.UseAnsi = False
        self.Slow = do_slow
        self.Identified = False
        self.SimulationMode = simulation_mode

        try:
            self.ftdi = ftdi.Ftdi()
        except Exception:
            print("Error openging FTDI device")
            self.ftdi = None

        if self.ftdi is not None:
            try:
                self.ftdi.open(0x0403, 0x6010, interface = 1)
            except Exception:
                traceback.print_exc(file = sys.stdout)

            if self.ftdi.is_connected:
                self.ftdi.set_bitmode(0, _resolve_mcu_bitmode(self.ftdi))

                if self.Slow:
                    # Clock FTDI chip at 12MHz instead of 60MHz
                    self.ftdi.write_data(Array('B', [ftdi.Ftdi.ENABLE_CLK_DIV5]))
                else:
                    self.ftdi.write_data(Array('B', [ftdi.Ftdi.DISABLE_CLK_DIV5]))

                self.ftdi.set_latency_timer(self.ftdi.LATENCY_MIN)
                self.ftdi.purge_buffers()
                self.ftdi.write_data(Array('B', [ftdi.Ftdi.SET_BITS_HIGH, 0x0, 0x1]))

        self.__wait_ready()
        self.__get_id()

    def _is_bad_block_marker(self, oob):
        return len(oob) > self.BadBlockMarkerOffset and oob[self.BadBlockMarkerOffset] != 0xff

    def __wait_ready(self):
        if self.ftdi is None or not self.ftdi.is_connected:
            return

        while True:
            self.ftdi.write_data(Array('B', [ftdi.Ftdi.GET_BITS_HIGH]))
            data = self.ftdi.read_data_bytes(1)
            if not data or len(data) <= 0:
                raise RuntimeError('FTDI device Not ready. Try restarting it.')

            if  data[0] & 2 == 0x2:
                return

            if self.Debug > 0:
                print('Not Ready', data)

        return

    def __read(self, cl, al, count):
        cmds = []
        cmd_type = 0
        if cl == 1:
            cmd_type |= flashdevice_defs.ADR_CL
        if al == 1:
            cmd_type |= flashdevice_defs.ADR_AL

        cmds += [ftdi.Ftdi.READ_EXTENDED, cmd_type, 0]

        for _ in range(1, count, 1):
            cmds += [ftdi.Ftdi.READ_SHORT, 0]

        cmds.append(ftdi.Ftdi.SEND_IMMEDIATE)

        if self.ftdi is None or not self.ftdi.is_connected:
            return b''

        self.ftdi.write_data(Array('B', cmds))
        if self.is_slow_mode():
            raw = bytearray()
            while len(raw) < count * 2:
                chunk = self.ftdi.read_data_bytes((count * 2) - len(raw), attempt=8)
                if not chunk:
                    break
                raw.extend(chunk)
            data = raw[0:-1:2]
        else:
            data = bytearray()
            while len(data) < count:
                chunk = self.ftdi.read_data_bytes(count - len(data), attempt=8)
                if not chunk:
                    break
                data.extend(chunk)

        if len(data) < count:
            raise RuntimeError('Short FTDI read: requested %d bytes, got %d bytes' % (count, len(data)))
        return bytes(data)

    def __write(self, cl, al, data):
        data = _as_bytes(data)
        if not data:
            return

        cmds = []
        cmd_type = 0
        if cl == 1:
            cmd_type |= flashdevice_defs.ADR_CL
        if al == 1:
            cmd_type |= flashdevice_defs.ADR_AL
        if not self.WriteProtect:
            cmd_type |= flashdevice_defs.ADR_WP

        cmds += [ftdi.Ftdi.WRITE_EXTENDED, cmd_type, 0, data[0]]
        for i in range(1, len(data), 1):
            #if i == 256:
            #    cmds += [Ftdi.WRITE_SHORT, 0, ord(data[i])]
            cmds += [ftdi.Ftdi.WRITE_SHORT, 0, data[i]]

        if self.ftdi is None or not self.ftdi.is_connected:
            return

        self.ftdi.write_data(Array('B', cmds))

    def __send_cmd(self, cmd):
        self.__write(1, 0, bytes([cmd]))

    def __send_address(self, addr, count):
        data = bytearray()

        for _ in range(0, count, 1):
            data.append(addr & 0xff)
            addr = addr>>8

        self.__write(0, 1, data)

    def __get_status(self):
        self.__send_cmd(0x70)
        status = self.__read_data(1)[0]
        return status

    def __read_data(self, count):
        return self.__read(0, 0, count)

    def __write_data(self, data):
        return self.__write(0, 0, data)

    def __get_id(self):
        self.Name = ''
        self.ID = 0
        self.PageSize = 0
        self.ChipSizeMB = 0
        self.EraseSize = 0
        self.Options = 0
        self.AddrCycles = 0

        self.__send_cmd(flashdevice_defs.NAND_CMD_READID)
        self.__send_address(0, 1)
        flash_identifiers = self.__read_data(8)

        if not flash_identifiers:
            return False

        manufacturer_id = flash_identifiers[0]
        device_id = flash_identifiers[1] if len(flash_identifiers) > 1 else flash_identifiers[0]

        for device_description in flashdevice_defs.DEVICE_DESCRIPTIONS:
            if device_description[1] == device_id:
                (self.Name, self.ID, self.PageSize, self.ChipSizeMB, self.EraseSize, self.Options, self.AddrCycles) = device_description
                self.Identified = True
                break

        if not self.Identified:
            return False

        #Check ONFI
        self.__send_cmd(flashdevice_defs.NAND_CMD_READID)
        self.__send_address(0x20, 1)
        onfitmp = self.__read_data(4)

        onfi = onfitmp == b'ONFI'
        onfi_geometry = None

        if onfi:
            self.__send_cmd(flashdevice_defs.NAND_CMD_ONFI)
            self.__send_address(0, 1)
            self.__wait_ready()
            onfi_data = self.__read_data(0x100)
            onfi = onfi_data[0:4] == b'ONFI'
            if onfi and len(onfi_data) >= 101:
                onfi_page_size = int.from_bytes(onfi_data[80:84], byteorder='little')
                onfi_oob_size = int.from_bytes(onfi_data[84:86], byteorder='little')
                onfi_pages_per_block = int.from_bytes(onfi_data[92:96], byteorder='little')
                onfi_blocks_per_lun = int.from_bytes(onfi_data[96:100], byteorder='little')
                onfi_lun_count = onfi_data[100]

                if (
                    onfi_page_size > 0 and
                    onfi_oob_size > 0 and
                    onfi_pages_per_block > 0 and
                    onfi_blocks_per_lun > 0 and
                    onfi_lun_count > 0
                ):
                    onfi_block_count = onfi_blocks_per_lun * onfi_lun_count
                    onfi_geometry = {
                        'page_size': onfi_page_size,
                        'oob_size': onfi_oob_size,
                        'pages_per_block': onfi_pages_per_block,
                        'block_count': onfi_block_count,
                    }

        if manufacturer_id == 0x98:
            self.Manufacturer = 'Toshiba'
        elif manufacturer_id == 0xec:
            self.Manufacturer = 'Samsung'
        elif manufacturer_id == 0x04:
            self.Manufacturer = 'Fujitsu'
        elif manufacturer_id == 0x8f:
            self.Manufacturer = 'National Semiconductors'
        elif manufacturer_id == 0x07:
            self.Manufacturer = 'Renesas'
        elif manufacturer_id == 0x20:
            self.Manufacturer = 'ST Micro'
        elif manufacturer_id == 0xad:
            self.Manufacturer = 'Hynix'
        elif manufacturer_id == 0x2c:
            self.Manufacturer = 'Micron'
        elif manufacturer_id == 0x01:
            self.Manufacturer = 'AMD'
        elif manufacturer_id == 0xc2:
            self.Manufacturer = 'Macronix'
        else:
            self.Manufacturer = 'Unknown'

        idstr = ''
        for idbyte in flash_identifiers:
            idstr += "%X" % idbyte
        if idstr[0:4] == idstr[-4:]:
            idstr = idstr[:-4]
            if idstr[0:2] == idstr[-2:]:
                idstr = idstr[:-2]
        self.IDString = idstr
        self.IDLength = len(idstr) // 2
        self.BitsPerCell = self.get_bits_per_cell(flash_identifiers[2])
        if self.PageSize == 0:
            extid = flash_identifiers[3]
            if ((self.IDLength == 6) and (self.Manufacturer == "Samsung") and (self.BitsPerCell > 1)):
                self.PageSize = 2048 << (extid & 0x03)
                extid >>= 2
                if (((extid >> 2) & 0x04) | (extid & 0x03)) == 1:
                    self.OOBSize = 128
                if (((extid >> 2) & 0x04) | (extid & 0x03)) == 2:
                    self.OOBSize = 218
                if (((extid >> 2) & 0x04) | (extid & 0x03)) == 3:
                    self.OOBSize = 400
                if (((extid >> 2) & 0x04) | (extid & 0x03)) == 4:
                    self.OOBSize = 436
                if (((extid >> 2) & 0x04) | (extid & 0x03)) == 5:
                    self.OOBSize = 512
                if (((extid >> 2) & 0x04) | (extid & 0x03)) == 6:
                    self.OOBSize = 640
                else:
                    self.OOBSize = 1024
                extid >>= 2
                self.EraseSize = (128 * 1024) << (((extid >> 1) & 0x04) | (extid & 0x03))
            elif ((self.IDLength == 6) and (self.Manufacturer == 'Hynix') and (self.BitsPerCell > 1)):
                self.PageSize = 2048 << (extid & 0x03)
                extid >>= 2
                if (((extid >> 2) & 0x04) | (extid & 0x03)) == 0:
                    self.OOBSize = 128
                elif (((extid >> 2) & 0x04) | (extid & 0x03)) == 1:
                    self.OOBSize = 224
                elif (((extid >> 2) & 0x04) | (extid & 0x03)) == 2:
                    self.OOBSize = 448
                elif (((extid >> 2) & 0x04) | (extid & 0x03)) == 3:
                    self.OOBSize = 64
                elif (((extid >> 2) & 0x04) | (extid & 0x03)) == 4:
                    self.OOBSize = 32
                elif (((extid >> 2) & 0x04) | (extid & 0x03)) == 5:
                    self.OOBSize = 16
                else:
                    self.OOBSize = 640
                tmp = ((extid >> 1) & 0x04) | (extid & 0x03)
                if tmp < 0x03:
                    self.EraseSize = (128 * 1024) << tmp
                elif tmp == 0x03:
                    self.EraseSize = 768 * 1024
                else: self.EraseSize = (64 * 1024) << tmp
            else:
                self.PageSize = 1024 << (extid & 0x03)
                extid >>= 2
                self.OOBSize = (8 << (extid & 0x01)) * (self.PageSize >> 9)
                extid >>= 2
                self.EraseSize = (64 * 1024) << (extid & 0x03)
                if ((self.IDLength >= 6) and (self.Manufacturer == "Toshiba") and (self.BitsPerCell > 1) and ((flash_identifiers[5] & 0x7) == 0x6) and not flash_identifiers[4] & 0x80):
                    self.OOBSize = 32 * self.PageSize >> 9
        else:
            self.OOBSize = self.PageSize // 32

        if onfi_geometry is not None:
            self.PageSize = onfi_geometry['page_size']
            self.OOBSize = onfi_geometry['oob_size']
            self.PagePerBlock = onfi_geometry['pages_per_block']
            self.BlockCount = onfi_geometry['block_count']
            self.BlockSize = self.PageSize * self.PagePerBlock
            self.PageCount = self.BlockCount * self.PagePerBlock
            self.RawPageSize = self.PageSize + self.OOBSize
            self.RawBlockSize = self.RawPageSize * self.PagePerBlock
            self.EraseSize = self.BlockSize
            self.ChipSizeMB = (self.PageCount * self.PageSize) // (1024 * 1024)
            return True

        if self.PageSize > 0:
            self.PageCount = (self.ChipSizeMB*1024*1024) // self.PageSize
        self.RawPageSize = self.PageSize + self.OOBSize
        self.BlockSize = self.EraseSize
        self.BlockCount = (self.ChipSizeMB*1024*1024) // self.BlockSize

        if self.BlockCount <= 0:
            self.PagePerBlock = 0
            self.RawBlockSize = 0
            return False

        self.PagePerBlock = self.PageCount // self.BlockCount
        self.RawBlockSize = self.PagePerBlock*(self.PageSize + self.OOBSize)

        return True

    def is_initialized(self):
        return self.Identified

    def set_use_ansi(self, use_ansi):
        self.UseAnsi = use_ansi

    def is_slow_mode(self):
        return self.Slow

    def get_bits_per_cell(self, cellinfo):
        bits = cellinfo & flashdevice_defs.NAND_CI_CELLTYPE_MSK
        bits >>= flashdevice_defs.NAND_CI_CELLTYPE_SHIFT
        return bits+1

    def dump_info(self):
        print('Full ID:\t', self.IDString)
        print('ID Length:\t', self.IDLength)
        print('Name:\t\t', self.Name)
        print('ID:\t\t0x%x' % self.ID)
        print('Page size:\t 0x{0:x}({0:d})'.format(self.PageSize))
        print('OOB size:\t0x{0:x} ({0:d})'.format(self.OOBSize))
        print('Page count:\t0x%x' % self.PageCount)
        print('Size:\t\t0x%x' % self.ChipSizeMB)
        print('Erase size:\t0x%x' % self.EraseSize)
        print('Block count:\t', self.BlockCount)
        print('Options:\t', self.Options)
        print('Address cycle:\t', self.AddrCycles)
        print('Bits per Cell:\t', self.BitsPerCell)
        print('Manufacturer:\t', self.Manufacturer)
        print('')

    def check_bad_blocks(self):
        bad_blocks = {}
#        end_page = self.PageCount

        if self.PageCount%self.PagePerBlock > 0.0:
            self.BlockCount += 1

        for block in range(0, self.BlockCount):
            page = block * self.PagePerBlock
            curblock = block + 1
            if self.UseAnsi:
                sys.stdout.write('Checking bad blocks %d Block: %d/%d\n\033[A' % (curblock / self.BlockCount*100.0, curblock, self.BlockCount))
            else:
                sys.stdout.write('Checking bad blocks %d Block: %d/%d\n' % (curblock / self.BlockCount*100.0, curblock, self.BlockCount))
            for pageoff in range(0, 2, 1):
                oob = self.read_oob(page+pageoff)

                if self._is_bad_block_marker(oob):
                    print('Bad block found:', block)
                    bad_blocks[page] = 1
                    break
        print('Checked %d blocks and found %d bad blocks' % (block+1, len(bad_blocks)))
        return bad_blocks

    def read_oob(self, pageno):
        data = bytearray()
        if self.Options & flashdevice_defs.LP_OPTIONS:
            self.__send_cmd(flashdevice_defs.NAND_CMD_READ0)
            # Large-page devices use column+row addressing; set column to PageSize for OOB.
            self.__send_address((pageno << 16) | self.PageSize, self.AddrCycles)
            self.__send_cmd(flashdevice_defs.NAND_CMD_READSTART)
            self.__wait_ready()
            data.extend(self.__read_data(self.OOBSize))
        else:
            self.__send_cmd(flashdevice_defs.NAND_CMD_READ_OOB)
            self.__wait_ready()
            self.__send_address(pageno<<8, self.AddrCycles)
            self.__wait_ready()
            data.extend(self.__read_data(self.OOBSize))

        return bytes(data)

    def read_page(self, pageno, remove_oob = False):
        bytes_to_read = bytearray()

        if self.Options & flashdevice_defs.LP_OPTIONS:
            self.__send_cmd(flashdevice_defs.NAND_CMD_READ0)
            self.__send_address(pageno<<16, self.AddrCycles)
            self.__send_cmd(flashdevice_defs.NAND_CMD_READSTART)
            self.__wait_ready()
            total_length = self.PageSize if remove_oob else (self.PageSize + self.OOBSize)
            if total_length > 0x1000:
                length = total_length
                while length > 0:
                    read_len = 0x1000
                    if length < 0x1000:
                        read_len = length
                    bytes_to_read.extend(self.__read_data(read_len))
                    length -= 0x1000
            else:
                bytes_to_read.extend(self.__read_data(total_length))
        else:
            self.__send_cmd(flashdevice_defs.NAND_CMD_READ0)
            self.__wait_ready()
            self.__send_address(pageno<<8, self.AddrCycles)
            self.__wait_ready()
            bytes_to_read.extend(self.__read_data(self.PageSize // 2))

            self.__send_cmd(flashdevice_defs.NAND_CMD_READ1)
            self.__wait_ready()
            self.__send_address(pageno<<8, self.AddrCycles)
            self.__wait_ready()
            bytes_to_read.extend(self.__read_data(self.PageSize // 2))

            if not remove_oob:
                self.__send_cmd(flashdevice_defs.NAND_CMD_READ_OOB)
                self.__wait_ready()
                self.__send_address(pageno<<8, self.AddrCycles)
                self.__wait_ready()
                bytes_to_read.extend(self.__read_data(self.OOBSize))

        return bytes(bytes_to_read)

    def read_seq(self, pageno, remove_oob = False, raw_mode = False):
        page = bytearray()
        self.__send_cmd(flashdevice_defs.NAND_CMD_READ0)
        self.__wait_ready()
        self.__send_address(pageno<<8, self.AddrCycles)
        self.__wait_ready()

        bad_block = False

        for i in range(0, self.PagePerBlock, 1):
            page_data = self.__read_data(self.RawPageSize)

            if i in (0, 1):
                marker_index = self.PageSize + self.BadBlockMarkerOffset
                if len(page_data) > marker_index and page_data[marker_index] != 0xff:
                    bad_block = True

            if remove_oob:
                page.extend(page_data[0:self.PageSize])
            else:
                page.extend(page_data)

            self.__wait_ready()

        if self.ftdi is None or not self.ftdi.is_connected:
            return b''

        self.ftdi.write_data(Array('B', [ftdi.Ftdi.SET_BITS_HIGH, 0x1, 0x1]))
        self.ftdi.write_data(Array('B', [ftdi.Ftdi.SET_BITS_HIGH, 0x0, 0x1]))

        data = b''

        if bad_block and not raw_mode:
            print('\nSkipping bad block at %d' % (pageno // self.PagePerBlock))
        else:
            data = bytes(page)

        return data

    def erase_block_by_page(self, pageno):
        self.WriteProtect = False
        self.__send_cmd(flashdevice_defs.NAND_CMD_ERASE1)
        self.__send_address(pageno, self.AddrCycles)
        self.__send_cmd(flashdevice_defs.NAND_CMD_ERASE2)
        self.__wait_ready()
        err = self.__get_status()
        self.WriteProtect = True

        return err

    def write_page(self, pageno, data):
        err = 0
        self.WriteProtect = False

        if self.Options & flashdevice_defs.LP_OPTIONS:
            self.__send_cmd(flashdevice_defs.NAND_CMD_SEQIN)
            self.__wait_ready()
            self.__send_address(pageno<<16, self.AddrCycles)
            self.__wait_ready()
            self.__write_data(data)
            self.__send_cmd(flashdevice_defs.NAND_CMD_PAGEPROG)
            self.__wait_ready()
        else:
            while True:
                self.__send_cmd(flashdevice_defs.NAND_CMD_READ0)
                self.__send_cmd(flashdevice_defs.NAND_CMD_SEQIN)
                self.__wait_ready()
                self.__send_address(pageno<<8, self.AddrCycles)
                self.__wait_ready()
                self.__write_data(data[0:256])
                self.__send_cmd(flashdevice_defs.NAND_CMD_PAGEPROG)
                err = self.__get_status()
                if err & flashdevice_defs.NAND_STATUS_FAIL:
                    print('Failed to write 1st half of ', pageno, err)
                    continue
                break

            while True:
                self.__send_cmd(flashdevice_defs.NAND_CMD_READ1)
                self.__send_cmd(flashdevice_defs.NAND_CMD_SEQIN)
                self.__wait_ready()
                self.__send_address(pageno<<8, self.AddrCycles)
                self.__wait_ready()
                self.__write_data(data[self.PageSize // 2:self.PageSize])
                self.__send_cmd(flashdevice_defs.NAND_CMD_PAGEPROG)
                err = self.__get_status()
                if err & flashdevice_defs.NAND_STATUS_FAIL:
                    print('Failed to write 2nd half of ', pageno, err)
                    continue
                break

            while True:
                self.__send_cmd(flashdevice_defs.NAND_CMD_READ_OOB)
                self.__send_cmd(flashdevice_defs.NAND_CMD_SEQIN)
                self.__wait_ready()
                self.__send_address(pageno<<8, self.AddrCycles)
                self.__wait_ready()
                self.__write_data(data[self.PageSize:self.RawPageSize])
                self.__send_cmd(flashdevice_defs.NAND_CMD_PAGEPROG)
                err = self.__get_status()
                if err & flashdevice_defs.NAND_STATUS_FAIL:
                    print('Failed to write OOB of ', pageno, err)
                    continue
                break

        self.WriteProtect = True
        return err

#    def write_block(self, block_data):
#        nand_tool.erase_block_by_page(0) #need to fix
#        page = 0
#        for i in range(0, len(data), self.RawPageSize):
#            nand_tool.write_page(pageno, data[i:i+self.RawPageSize])
#            page += 1

    def write_pages(self, filename, offset = 0, start_page = -1, end_page = -1, add_oob = False, add_jffs2_eraser_marker = False, raw_mode = False):
        with open(filename, 'rb') as fd:
            fd.seek(offset)
            data = fd.read()

        if start_page == -1:
            start_page = 0

        if end_page == -1:
            end_page = self.PageCount-1

        end_block = end_page // self.PagePerBlock

        if end_page % self.PagePerBlock > 0:
            end_block += 1

        start = time.time()
        ecc_calculator = ecc.Calculator()

        page = start_page
        block = page // self.PagePerBlock
        current_data_offset = 0
        length = 0

        while page <= end_page and current_data_offset < len(data) and block < self.BlockCount:
            oob_postfix = b'\xff' * 13
            if page%self.PagePerBlock == 0:

                if not raw_mode:
                    bad_block_found = False
                    for pageoff in range(0, 2, 1):
                        oob = self.read_oob(page+pageoff)

                        if self._is_bad_block_marker(oob):
                            bad_block_found = True
                            break

                    if bad_block_found:
                        print('\nSkipping bad block at ', block)
                        page += self.PagePerBlock
                        block += 1
                        continue

                if add_jffs2_eraser_marker:
                    oob_postfix = b"\xFF\xFF\xFF\xFF\xFF\x85\x19\x03\x20\x08\x00\x00\x00"

                self.erase_block_by_page(page)

            if add_oob:
                orig_page_data = data[current_data_offset:current_data_offset + self.PageSize]
                current_data_offset += self.PageSize
                length += len(orig_page_data)
                orig_page_data += (self.PageSize - len(orig_page_data)) * b'\x00'
                (ecc0, ecc1, ecc2) = ecc_calculator.calc(orig_page_data)

                oob = struct.pack('BBB', ecc0, ecc1, ecc2) + oob_postfix
                page_data = orig_page_data+oob
            else:
                page_data = data[current_data_offset:current_data_offset + self.RawPageSize]
                current_data_offset += self.RawPageSize
                length += len(page_data)

            if len(page_data) != self.RawPageSize:
                print('Not enough source data')
                break

            current = time.time()

            if end_page == start_page:
                progress = 100
            else:
                progress = (page-start_page) * 100 / (end_page-start_page)

            lapsed_time = current-start

            if lapsed_time > 0:
                if self.UseAnsi:
                    sys.stdout.write('Writing %d%% Page: %d/%d Block: %d/%d Speed: %d bytes/s\n\033[A' % (progress, page, end_page, block, end_block, length/lapsed_time))
                else:
                    sys.stdout.write('Writing %d%% Page: %d/%d Block: %d/%d Speed: %d bytes/s\n' % (progress, page, end_page, block, end_block, length/lapsed_time))
            self.write_page(page, page_data)

            if page%self.PagePerBlock == 0:
                block = page // self.PagePerBlock
            page += 1

        print('\nWritten %x bytes / %x byte' % (length, len(data)))

    def erase(self):
        block = 0
        while block < self.BlockCount:
            self.erase_block_by_page(block * self.PagePerBlock)
            block += 1

    def erase_block(self, start_block, end_block):
        print('Erasing Block: 0x%x ~ 0x%x' % (start_block, end_block))
        for block in range(start_block, end_block+1, 1):
            print("Erasing block", block)
            self.erase_block_by_page(block * self.PagePerBlock)
