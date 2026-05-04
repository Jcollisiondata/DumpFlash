# pylint: disable=invalid-name
# pylint: disable=line-too-long
import sys
from argparse import ArgumentParser

import flashimage
import jffs2
import uboot


def build_parser():
    parser = ArgumentParser()

    parser.add_argument("-c", dest="command", default="information", help="Command (i[nformation], r[ead], s[equential_read], w[rite], erase, e[xtract], extract_pages, add_oob, remove_oob, check_ecc, find_uboot, dump_uboot, find_jffs2, dump_jffs2, check_bad_blocks)")
    parser.add_argument("-i", dest="raw_image_filename", default="", help="Use file instead of device for operations")
    parser.add_argument("-o", dest="output_filename", default="output.dmp", help="Output filename")

    parser.add_argument("-L", action="store_true", dest="slow", default=False, help="Set clock FTDI chip at 12MHz instead of 60MHz")
    parser.add_argument("-R", action="store_true", dest="raw_mode", default=False, help="Raw mode - skip bad block before reading/writing")

    parser.add_argument("-j", action="store_true", dest="add_jffs2_oob", default=False, help="Add JFFS2 OOB to the source")
    parser.add_argument("-C", dest="compare_target_filename", default="", help="When writing a file compare with this file before writing and write only differences", metavar="COMPARE_TARGET_FILENAME")

    parser.add_argument("-n", dest="name_prefix", default="", help="Set output file name prefix")

    parser.add_argument("-s", type=int, default=0, dest="start_offset")
    parser.add_argument("-l", type=int, default=0, dest="length")
    parser.add_argument("-p", type=int, nargs=2, dest="pages")
    parser.add_argument("-b", type=int, nargs=2, dest="blocks")

    parser.add_argument("-P", type=int, default=512, dest="page_size")
    parser.add_argument("-O", type=int, default=16, dest="oob_size")
    parser.add_argument("--bp", type=int, default=32, dest="pages_per_block")
    parser.add_argument("extra_args", nargs="*")
    return parser


def _enable_ansi():
    try:
        import colorama
    except ImportError:
        try:
            import tendo.ansiterm  # noqa: F401
        except ImportError:
            return False
        return True

    colorama.init()
    return True


def _apply_page_selection(options, flash_image_io):
    start_page = -1
    end_page = -1

    if options.pages is not None:
        start_page = options.pages[0]
        end_page = options.pages[1]

    if options.blocks is not None:
        start_page = options.blocks[0] * flash_image_io.SrcImage.PagePerBlock
        end_page = (options.blocks[1] + 1) * flash_image_io.SrcImage.PagePerBlock

    return start_page, end_page


def _write_differences(options, flash_image_io, filename, start_page, add_oob, add_jffs2_eraser_marker):
    with open(options.compare_target_filename, "rb") as cfd, open(filename, "rb") as fd:
        cfd.seek(options.start_offset)
        fd.seek(options.start_offset)

        current_page = 0
        while True:
            cdata = cfd.read(flash_image_io.SrcImage.PageSize)
            data = fd.read(flash_image_io.SrcImage.PageSize)

            if not data:
                break

            if cdata != data:
                print("Changed Page:0x%x file_offset: 0x%x" % (start_page + current_page, options.start_offset + current_page * flash_image_io.SrcImage.PageSize))
                current_block = current_page // flash_image_io.SrcImage.PagePerBlock

                print("Erasing and re-programming Block: %d" % current_block)
                flash_image_io.SrcImage.erase_block_by_page(current_page)

                target_start_page = start_page + current_block * flash_image_io.SrcImage.PagePerBlock
                target_end_page = target_start_page + flash_image_io.SrcImage.PagePerBlock - 1

                print("Programming Page: %d ~ %d" % (target_start_page, target_end_page))
                flash_image_io.SrcImage.write_pages(
                    filename,
                    options.start_offset + current_block * flash_image_io.SrcImage.PagePerBlock * flash_image_io.SrcImage.PageSize,
                    target_start_page,
                    target_end_page,
                    add_oob,
                    add_jffs2_eraser_marker=add_jffs2_eraser_marker,
                    raw_mode=options.raw_mode,
                )

                current_page = (current_block + 1) * flash_image_io.SrcImage.PagePerBlock + 1
                fd.seek(options.start_offset + current_page * flash_image_io.SrcImage.PageSize)
                cfd.seek(options.start_offset + current_page * flash_image_io.SrcImage.PageSize)
            else:
                current_page += 1


def main(argv=None):
    parser = build_parser()
    options = parser.parse_args(argv)
    command = options.command

    flash_image_io = flashimage.IO(
        options.raw_image_filename,
        options.start_offset,
        options.length,
        options.page_size,
        options.oob_size,
        options.pages_per_block,
        options.slow,
    )

    if not flash_image_io.is_initialized():
        print("Device not ready, aborting...")
        return 1

    flash_image_io.set_use_ansi(_enable_ansi())
    start_page, end_page = _apply_page_selection(options, flash_image_io)

    if command[0] == "i":
        flash_image_io.SrcImage.dump_info()

    elif command == "add_oob":
        if options.raw_image_filename:
            print("Add OOB to %s" % options.raw_image_filename)
            flash_image_io.add_oob(options.raw_image_filename, options.output_filename)

    elif command == "extract_pages":
        if options.raw_image_filename:
            print("Extract from pages(0x%x - 0x%x) to %s" % (start_page, end_page, options.output_filename))
            flash_image_io.extract_pages(options.output_filename, start_page, end_page, remove_oob=False)

    elif command == "remove_oob" or (command[0] == "e" and command != "erase"):
        if options.raw_image_filename:
            print("Extract data from pages(0x%x - 0x%x) to %s" % (start_page, end_page, options.output_filename))
            flash_image_io.extract_pages(options.output_filename, start_page, end_page, remove_oob=True)

    elif command == "erase":
        if options.blocks is not None:
            flash_image_io.SrcImage.erase_block(options.blocks[0], options.blocks[1])
        else:
            flash_image_io.SrcImage.erase()

    elif command[0] in ("r", "s"):
        sequential_read = command[0] == "s"
        flash_image_io.read_pages(start_page, end_page, False, options.output_filename, seq=sequential_read, raw_mode=options.raw_mode)

    elif command[0] == "w":
        if not options.extra_args:
            parser.error("write command requires an input filename")

        filename = options.extra_args[0]
        add_oob = command == "add_oob"
        add_jffs2_eraser_marker = False

        if options.add_jffs2_oob:
            add_oob = True
            add_jffs2_eraser_marker = True

        if options.compare_target_filename:
            _write_differences(options, flash_image_io, filename, start_page, add_oob, add_jffs2_eraser_marker)
        else:
            flash_image_io.SrcImage.write_pages(filename, options.start_offset, start_page, end_page, add_oob, add_jffs2_eraser_marker=add_jffs2_eraser_marker, raw_mode=options.raw_mode)

    elif command == "check_bad_blocks":
        flash_image_io.check_bad_blocks()

    elif command == "check_ecc":
        flash_image_io.check_ecc()

    elif command == "find_uboot":
        uboot.Util(flash_image_io).find()

    elif command == "dump_uboot":
        uboot.Util(flash_image_io).dump()

    elif command == "find_jffs2":
        jffs2.Util(flash_image_io).find()

    elif command == "dump_jffs2":
        jffs2.Util(flash_image_io).dump(options.name_prefix)

    else:
        parser.error("unknown command: %s" % command)

    return 0


if __name__ == "__main__":
    sys.exit(main())
