---
title: "Move your own S3 storage back to MOD storage"
description: "Move the files in your bring-your-own S3 bucket out to MOD storage, then switch back. A file that was in the bucket when the move started and is still there blocks the switch until you delete it yourself."
---

# Move your own S3 storage back to MOD storage

When you connect your own S3-compatible bucket, MOD serves your files from that
bucket. If you later want MOD to serve them from its own storage instead — to
close the connection, change your endpoint, or stop using your bucket — MOD
first asks you to move the files out. Deactivating while your bucket still
holds files would leave them unreachable from MOD, so MOD refuses until the
move is done.

The move tool copies your files into MOD storage. It never deletes anything.

## What the move does

- MOD lists your bucket and copies every object it finds into MOD storage,
  in chunks, while your connection stays active.
- Files MOD already knew are stored under their content hash, so a file that
  already exists in both places is the same bytes — re-running the move
  changes nothing. Other files in your bucket keep their own names and are
  copied as they are.
- Your bucket keeps everything it had. Files stay in your bucket,
  unreachable from MOD, until you delete them yourself.
- Nothing is deleted by MOD at any point in this flow, and no file count or
  bucket size limits the move.

## Steps

1. **Start the move.** In Settings, on your storage connection, click
   Deactivate. MOD stops and asks "Move the files out first?" and shows
   where the files go: MOD storage (the default), or one of your other
   connections. You then get a count of the objects MOD found in your
   bucket.
2. **Watch progress.** The same dialog shows objects moved, bytes moved,
   and how many chunks are done. You can keep using MOD while the move
   runs — the connection is still active and still serves your files.
3. **If a chunk fails**, the move is marked failed, your connection is
   untouched, and you can start the move again. A retry copies the bucket
   again from the start. Files that are already in MOD storage are written
   again with the same bytes, so nothing changes, but the retry is not
   cheap: it transfers every file again.
4. **Cancel if you change mind.** Cancelling stops the queued chunks and
   records the move as cancelled in your history. Already-copied files
   stay in MOD storage; your bucket is unchanged.
5. **Switch back.** Once the move is verified, deactivate the connection.
   Before you deactivate, delete the files MOD stored in your bucket yourself,
   in your own tools: MOD never deletes anything in your bucket.
   MOD checks the files that were in your bucket when the move started. If one
   of those is still there, the switch is refused; delete it in your own tools
   and try again. Files you added after the move started are not checked.

If MOD still refuses and you have already deleted the files in your own tools, choose "I deleted them myself". MOD re-checks your bucket before it switches; the choice does not skip that check.

## If you add a file during the move

Your connection is still active while the move runs, so a new upload lands
in your bucket and is not part of the move. A file you upload after a move
starts is not on that move's list, so the move does not copy it, and the
switch does not check it. Run the move again: it lists your bucket again and
includes the new file. Deactivate after the newest move shows verified.

## What MOD never does here

- MOD never deletes an object from your bucket, and never empties your
  bucket. Deleting your own files is your decision, made in your own tools.
- MOD never asks you to move everything before it will let you use a
  feature; the move is about reachability, not permission.
- MOD never moves files across a data-residency pin. If your region is
  pinned, a move that would cross the pin is refused and your data stays
  where it is.

## Troubleshooting

| You see | What it means | What to do |
|---|---|---|
| "Your bring-your-own S3 storage still holds at least one object" | The move has not finished, or a file that was in your bucket when the move started is still there | Run the move, then deactivate |
| "A move-out for this connection is already in flight" | A move is still running | Watch it with the progress dialog, or cancel it first |
| "Pick exactly one move-out target" | You asked for two destinations at once | Choose MOD storage or one of your connections |
| "Connection cannot be used as a move-out target until a connection test passes" | The destination connection has no passing test | Run the connection test first |
| Move marked failed | A copy failed mid-move | Start the move again. Files are copied again, so a retry takes as long as the first run |
