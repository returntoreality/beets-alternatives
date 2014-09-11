beets-alternatives
==================

[![Build Status](https://travis-ci.org/geigerzaehler/beets-alternatives.svg?branch=master)](https://travis-ci.org/geigerzaehler/beets-alternatives)
[![Coverage Status](https://coveralls.io/repos/geigerzaehler/beets-alternatives/badge.png?branch=master)](https://coveralls.io/r/geigerzaehler/beets-alternatives?branch=master)

You want to manage multiple versions of your audio files with beets?
Your favorite iPlayer has limited space and does not support OGG? You
want to keep lossless versions on a large external drive? You want to
symlink your audio to other locations?

Getting Started
---------------

The basic idea of this plugin is that every file in your library can
have multiple alternate versions in separate locations.

There are three basic use cases introduced below.

### External Files

Suppose your favorite portable player only supports MP3 and has
limited disk space. It is mounted at `/player` and instead of selecting
its content manually and using the `convert` plugin to transcode it you
want to sync it automatically. We call this external location
'myplayer' and start configuring beets.

```yaml
alternatives:
  myplayer:
    directory: /player
    paths:
      default: $album/$title
    format: mp3
    query: "onplayer:true"
    removable: true
```

The first to options are self-explanatory. They determine the location
of the external files and correspond to the global
[`directory`][config-directory] and [`paths`][config-paths] options.
The `format` option specifies the format we transcode the files to.
We use the [convert plugin][], so the format name must correspond to
one of the formats [configured for convert][]. Finally, the `query`
option tells the plugin which files you want to put in the external
location. The value is a [query string][] as used for the beets command
line. In our case we use a flexible attribute to make the selection
transparent.

First we add some files to our selection by setting the flexible
attribute from the `query` option.

```
$ beet modify onplayer=true artist:Bach
```

We then tell beets to create the external files.

```
$ beet alt update myplayer
Collection at '/player' does not exists. Maybe you forgot to mount it.
Do you want to create the collection? (y/N)
```

The question makes sure that you don’t recreate a external collection
if the device is not mounted. Since this is our first go, we answer the
question by typing `y`.  A quick look into the `/player` directory then
reveals that indeed all tracks of Bach have been transcoded to MP3 and
copied to the player.

If you update your takes locally, the `alt update` command will
propagate the changes to your external collection. Since we don’t need
to convert the files but just update the tags, this will be much faster
the second time.

```
$ beet modify composer="Johann Sebastian Bach" artist:Bach
$ beet alt update myplayer
```

After going for a run you realize that Bach is probably not the right
thing to work out to. So you decide to put Beethoven on your player.

```
$ beet modify onplayer! artist:Bach
$ beet modify onplayer=true artist:Beethoven
$ beet alt update myplayer
```

This removes all Bach tracks from the player and adds Beethoven’s.

### Symlink Views

Instead of copying and converting files this plugin can also create
symbolic links to the files in your library. For example you want to
have a directory containing all music sorted by year and album.

```yaml
directory: /music
paths:
  default: $artist/$album/$title

alternatives:
  by-year:
    directory: by-year
    paths:
      default: $year/$album/$title
    format: link
```

The first thing to note here is the `link` format. Instead of
converting the files this tells the plugin to create symbolic links to
the original audio file.  We also note that the directory is a relative
path: it will be resolved with respect to the global `directory`
option.  Finally, we omitted the `query` option. This means that we
want to create symlinks for all files. Of course you can still add a
query to select only parts of your collection.

The `beet alt update by-year` command will now create the symlinks. For
example

```
/music/by-year/1982/Thriller/Beat It.mp3
-> /music/Michael Jackson/Thriller/Beat It.mp3
```


### Archive Files

```yaml
alternatives:
  lossless:
    directory: /archive/music
    paths: ...
    archive: 'format:FLAC'
    keep: mp3
```


CLI Reference
-------------

```
beet alt update [options] NAME
```

Updates the external collection configured under `alt.external.NAME`.

* Add missing files. Convert them to the configured format or copy
  them.

* Remove files that don’t match the query but are still in the
  external collection

* Move files to the path determined from the `paths` configuration.

* Update tags if the modification time of the external file is older
  then that of the source file from the library.

* **`--[no-]create`** If the `removable` configuration option
  is set and the external base directory does not exists, then the
  command will ask you to confirm the creation of the external
  collection. These options specify the answer as a cli option.

Configuration
-------------

The `alt.external` configuration is a dictionary. The keys are the
names of the external locations and used for reference from the command
line. The values are again dictionaries with the following keys.

* **`directory`** The root directory to store the external files under.
  Relative paths are resolved with respect to the global `directory`
  configuration.

* **`paths`** Path templates for audio files under `directory`. Configured
  like and defaults to [global paths option][config-paths].

* **`query`** A [query string][] that determine which tracks belong to the
  collection.

* **`format`** (optional) A string that determines the format to convert
  audio files in the external collection to. The string must correspond
  to a key in the [`convert.formats`][convert plugin] configuration.
  The settings of the configuration are used to run the conversion.

* **`removable`** If this is `true` (the default) and `directory` does
  not exist, the `update` command will ask you to confirm the creation
  of the external collection.


Feature Requests
----------------

If you have an idea or a use case this plugin is missing feel free to
[open an issue](https://github.com/geigerzaehler/beets-alternatives/issues/new).

The following is a list of things I might add in the feature.

* Symbolic links for each artist in a multiple artist release (see the
  [beets issue][beets-issue-split-symlinks])

License
-------

Copyright (c) 2014 Thomas Scholtes.

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"), to
deal in the Software without restriction, including without limitation the
rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
sell copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.


[beets-issue-split-symlinks]: https://github.com/sampsyo/beets/issues/153
[config-directory]: http://beets.readthedocs.org/en/latest/reference/config.html#directory
[config-paths]: http://beets.readthedocs.org/en/latest/reference/config.html#path-format-configuration
[configured for convert]: http://beets.readthedocs.org/en/latest/plugins/convert.html#configuring-the-transcoding-command
[convert plugin]: http://beets.readthedocs.org/en/latest/plugins/convert.html
[query string]: http://beets.readthedocs.org/en/latest/reference/query.html
