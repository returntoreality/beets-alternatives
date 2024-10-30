# Copyright (c) 2014 Thomas Scholtes

# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the "Software"), to
# deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
# sell copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

import argparse
import logging
import os.path
import shutil
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent import futures
from enum import Enum
from pathlib import Path

import beets
import confuse
from beets import art, util
from beets.library import Item, Library, parse_query_string
from beets.plugins import BeetsPlugin
from beets.ui import Subcommand, UserError, decargs, get_path_formats, input_yn, print_
from typing_extensions import Never, override

import beetsplug.convert as convert


class AlternativesPlugin(BeetsPlugin):
    def __init__(self):
        super().__init__()

    def commands(self):  # pyright: ignore[reportIncompatibleMethodOverride]
        return [AlternativesCommand(self)]

    def update(self, lib: Library, options: argparse.Namespace):
        if options.name is None:
            if not options.all:
                raise UserError("Please specify a collection name or the --all flag")

            for name in self.config.keys():  # noqa: SIM118
                self.alternative(name, lib).update(create=options.create)
        else:
            try:
                alt = self.alternative(options.name, lib)
            except KeyError as e:
                raise UserError(
                    f"Alternative collection '{e.args[0]}' not found."
                ) from e
            alt.update(create=options.create)

    def list_tracks(self, lib: Library, options: argparse.Namespace):
        if options.format is not None:
            (fmt,) = decargs([options.format])
            beets.config[Item._format_config_key].set(fmt)  # pyright: ignore[reportPrivateUsage]

        alt = self.alternative(options.name, lib)

        # This is slow but we cannot use a native SQL query since the
        # path key is a flexible attribute
        for item in lib.items():
            if alt.path_key in item:
                print_(format(item))

    def alternative(self, name: str, lib: Library):
        conf = self.config[name]
        if not conf.exists():
            raise KeyError(name)

        if conf["formats"].exists():
            fmt = conf["formats"].as_str()
            assert isinstance(fmt, str)
            if fmt == "link":
                return SymlinkView(self._log, name, lib, conf)
            else:
                return ExternalConvert(self._log, name, fmt.split(), lib, conf)
        else:
            return External(self._log, name, lib, conf)


class AlternativesCommand(Subcommand):
    name = "alt"
    help = "manage alternative files"

    def __init__(self, plugin: AlternativesPlugin):
        parser = ArgumentParser()
        subparsers = parser.add_subparsers(prog=parser.prog + " alt")
        subparsers.required = True

        update = subparsers.add_parser("update")
        update.set_defaults(func=plugin.update)
        update.add_argument(
            "name",
            metavar="NAME",
            nargs="?",
            help="Name of the collection. Must be  provided unless --all is given",
        )
        update.add_argument("--create", action="store_const", dest="create", const=True)
        update.add_argument(
            "--no-create", action="store_const", dest="create", const=False
        )
        update.add_argument(
            "--all",
            action="store_true",
            default=False,
            help="Update all alternative collections that are defined in the configuration",
        )

        list_tracks = subparsers.add_parser(
            "list-tracks",
            description="""
                List all tracks that are currently part of an alternative
                collection""",
        )
        list_tracks.set_defaults(func=plugin.list_tracks)
        list_tracks.add_argument(
            "name",
            metavar="NAME",
            help="Name of the alternative",
        )
        list_tracks.add_argument(
            "-f",
            "--format",
            metavar="FORMAT",
            dest="format",
            help="""Format string to print for each track. See beets’
                Path Formats for more information.""",
        )

        super().__init__(self.name, parser, self.help)

    def func(self, lib: Library, opts: argparse.Namespace, _):  # pyright: ignore[reportIncompatibleMethodOverride]
        opts.func(lib, opts)

    def parse_args(self, args: Sequence[str]):  # pyright: ignore
        return self.parser.parse_args(args), []


class ArgumentParser(argparse.ArgumentParser):
    """
    Facade for ``argparse.ArgumentParser`` so that beets can call
    `_get_all_options()` to generate shell completion.
    """

    def _get_all_options(self) -> Sequence[Never]:
        # FIXME return options like ``OptionParser._get_all_options``.
        return []


class Action(Enum):
    ADD = 1
    REMOVE = 2
    WRITE = 3
    MOVE = 4
    SYNC_ART = 5


class External:
    def __init__(
        self, log: logging.Logger, name: str, lib: Library, config: confuse.ConfigView
    ):
        self._log = log
        self.name = name
        self.lib = lib
        self.path_key = f"alt.{name}"
        self.max_workers = int(str(beets.config["convert"]["threads"]))
        self.parse_config(config)

    def parse_config(self, config: confuse.ConfigView):
        if "paths" in config:
            path_config = config["paths"]
        else:
            path_config = beets.config["paths"]
        self.path_formats = get_path_formats(path_config)
        query = config["query"].as_str()
        self.query, _ = parse_query_string(query, Item)

        self.removable = config.get(dict).get("removable", True)  # type: ignore

        if "directory" in config:
            dir = config["directory"].as_path()
            assert isinstance(dir, Path)
        else:
            dir = Path(self.name)
        if not dir.is_absolute():
            dir = Path(str(self.lib.directory, "utf8")) / dir  # type: ignore
        self.directory = dir

    def item_change_actions(
        self, item: Item, actual: Path, dest: Path
    ) -> Sequence[Action]:
        """Returns the necessary actions for items that were previously in the
        external collection, but might require metadata updates.
        """
        actions = []

        if actual != dest:
            actions.append(Action.MOVE)

        item_mtime_alt = actual.stat().st_mtime
        if item_mtime_alt < Path(str(item.path, "utf8")).stat().st_mtime:
            actions.append(Action.WRITE)
        album = item.get_album()

        if (
            album
            and album.artpath
            and Path(str(album.artpath, "utf8")).is_file()
            and (item_mtime_alt < Path(str(album.artpath, "utf8")).stat().st_mtime)
        ):
            actions.append(Action.SYNC_ART)

        return actions

    def _matched_item_action(self, item: Item) -> Sequence[Action]:
        actual = self._get_stored_path(item)
        if actual and (actual.is_file() or actual.is_symlink()):
            dest = self.destination(item)
            if actual.suffix == dest.suffix:
                return self.item_change_actions(item, actual, dest)
            else:
                # formats config option changed
                return [Action.REMOVE, Action.ADD]
        else:
            return [Action.ADD]

    def _items_actions(self) -> Iterator[tuple[Item, Sequence[Action]]]:
        matched_ids = set()
        for album in self.lib.albums():
            if self.query.match(album):
                matched_items = album.items()
                matched_ids.update(item.id for item in matched_items)

        for item in self.lib.items():
            if item.id in matched_ids or self.query.match(item):
                yield (item, self._matched_item_action(item))
            elif self._get_stored_path(item):
                yield (item, [Action.REMOVE])

    def ask_create(self, create: bool | None = None) -> bool:
        if not self.removable:
            return True
        if create is not None:
            return create

        msg = (
            f"Collection at '{self.directory}' does not exists. "
            "Maybe you forgot to mount it.\n"
            "Do you want to create the collection? (y/n)"
        )
        return input_yn(msg, require=True)

    def update(self, create: bool | None = None):
        if not self.directory.is_dir() and not self.ask_create(create):
            print_(f"Skipping creation of {self.directory}")
            return

        converter = self._converter()
        for item, actions in self._items_actions():
            dest = self.destination(item)
            path = self._get_stored_path(item)
            for action in actions:
                if action == Action.MOVE:
                    assert path is not None  # action guarantees that `path` is not none
                    print_(f">{path} -> {dest}")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    path.rename(dest)
                    # beets types are confusing
                    util.prune_dirs(str(path.parent), root=str(self.directory))  # pyright: ignore
                    self._set_stored_path(item, dest)
                    item.store()
                    path = dest
                elif action == Action.WRITE:
                    assert path is not None  # action guarantees that `path` is not none
                    print_(f"*{path}")
                    item.write(path=bytes(path))
                elif action == Action.SYNC_ART:
                    print_(f"~{path}")
                    assert path is not None
                    self._sync_art(item, path)
                elif action == Action.ADD:
                    print_(f"+{dest}")
                    converter.run(item)
                elif action == Action.REMOVE:
                    assert path is not None  # action guarantees that `path` is not none
                    print_(f"-{path}")
                    self._remove_file(item)
                    item.store()

        for item, dest in converter.as_completed():
            self._set_stored_path(item, dest)
            item.store()
        converter.shutdown()

    def destination(self, item: Item) -> Path:
        """Returns the path for `item` in the external collection."""
        path = item.destination(path_formats=self.path_formats, fragment=True)
        # When using `fragment=True` the returned path is guaranteed to be a
        # string.
        assert isinstance(path, str)
        return self.directory / path

    def _set_stored_path(self, item: Item, path: Path):
        item[self.path_key] = str(path)

    def _get_stored_path(self, item: Item) -> Path | None:
        try:
            path = item[self.path_key]
        except KeyError:
            return None

        if isinstance(path, str):
            return Path(path)
        else:
            return None

    def _remove_file(self, item: Item):
        """Remove the external file for `item`."""
        path = self._get_stored_path(item)
        if path:
            path.unlink(missing_ok=True)
            # beets types are confusing
            util.prune_dirs(str(path), root=str(self.directory))  # pyright: ignore
        del item[self.path_key]

    def _converter(self) -> "Worker":
        def _convert(item: Item):
            dest = self.destination(item)
            dest.parent.mkdir(exist_ok=True, parents=True)
            shutil.copyfile(item.path, dest)
            return item, dest

        return Worker(_convert, self.max_workers)

    def _sync_art(self, item: Item, path: Path):
        """Embed artwork in the file at `path`."""
        album = item.get_album()
        if album and album.artpath and Path(str(album.artpath, "utf8")).is_file():
            self._log.debug(f"Embedding art from {album.artpath} into {path}")
            art.embed_item(self._log, item, album.artpath, itempath=bytes(path))


class ExternalConvert(External):
    def __init__(
        self,
        log: logging.Logger,
        name: str,
        formats: Iterable[str],
        lib: Library,
        config: confuse.ConfigView,
    ):
        super().__init__(log, name, lib, config)
        convert_plugin = convert.ConvertPlugin()
        self._encode = convert_plugin.encode
        self._embed = convert_plugin.config["embed"].get(bool)
        formats = [f.lower() for f in formats]
        self.formats = [convert.ALIASES.get(f, f) for f in formats]
        self.convert_cmd, self.ext = convert.get_format(self.formats[0])

    @override
    def _converter(self) -> "Worker":
        fs_lock = threading.Lock()

        def _convert(item: Item):
            dest = self.destination(item)
            with fs_lock:
                dest.parent.mkdir(exist_ok=True, parents=True)

            if self._should_transcode(item):
                self._encode(self.convert_cmd, item.path, bytes(dest))
                # Don't rely on the converter to write correct/complete tags.
                item.write(path=bytes(dest))
            else:
                self._log.debug(f"copying {dest}")
                shutil.copyfile(item.path, dest)
            if self._embed:
                self._sync_art(item, dest)
            return item, dest

        return Worker(_convert, self.max_workers)

    @override
    def destination(self, item: Item) -> Path:
        dest = super().destination(item)
        if self._should_transcode(item):
            return dest.with_suffix("." + self.ext.decode("utf8"))
        else:
            return dest

    def _should_transcode(self, item: Item):
        return item.format.lower() not in self.formats


class SymlinkType(Enum):
    ABSOLUTE = 0
    RELATIVE = 1


class SymlinkView(External):
    @override
    def parse_config(self, config: confuse.ConfigView):
        if "query" not in config:
            config["query"] = ""  # This is a TrueQuery()
        if "link_type" not in config:
            # Default as absolute so it doesn't break previous implementation
            config["link_type"] = "absolute"

        self.relativelinks = config["link_type"].as_choice({
            "relative": SymlinkType.RELATIVE,
            "absolute": SymlinkType.ABSOLUTE,
        })

        super().parse_config(config)

    @override
    def item_change_actions(
        self, item: Item, actual: Path, dest: Path
    ) -> Sequence[Action]:
        """Returns the necessary actions for items that were previously in the
        external collection, but might require metadata updates.
        """

        if (
            actual == dest
            and actual.is_file()  # Symlink not broken, `.samefile()` doesn’t throw
            and actual.samefile(Path(str(item.path, "utf8")))
        ):
            return []
        else:
            return [Action.MOVE]

    @override
    def update(self, create: bool | None = None):
        for item, actions in self._items_actions():
            dest = self.destination(item)
            path = self._get_stored_path(item)
            for action in actions:
                if action == Action.MOVE:
                    assert path is not None  # action guarantees that `path` is not none
                    print_(f">{path} -> {dest}")
                    self._remove_file(item)
                    self._create_symlink(item)
                    self._set_stored_path(item, dest)
                elif action == Action.ADD:
                    print_(f"+{dest}")
                    self._create_symlink(item)
                    self._set_stored_path(item, dest)
                elif action == Action.REMOVE:
                    assert path is not None  # action guarantees that `path` is not none
                    print_(f"-{path}")
                    self._remove_file(item)
                else:
                    continue
                item.store()

    def _create_symlink(self, item: Item):
        dest = self.destination(item)
        dest.parent.mkdir(exist_ok=True, parents=True)
        item_path = Path(str(item.path, "utf8"))
        link = (
            os.path.relpath(item_path, dest.parent)
            if self.relativelinks == SymlinkType.RELATIVE
            else item_path
        )
        dest.symlink_to(link)

    @override
    def _sync_art(self, item: Item, path: Path):
        pass


class Worker(futures.ThreadPoolExecutor):
    def __init__(
        self, fn: Callable[[Item], tuple[Item, Path]], max_workers: int | None
    ):
        super().__init__(max_workers)
        self._tasks: set[futures.Future[tuple[Item, Path]]] = set()
        self._fn = fn

    def run(self, item: Item):
        fut = self.submit(self._fn, item)
        self._tasks.add(fut)
        return fut

    def as_completed(self):
        for f in futures.as_completed(self._tasks):
            self._tasks.remove(f)
            yield f.result()
