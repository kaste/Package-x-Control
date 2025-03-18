This is an add-on to Package Control, hence the X in the name.

## Goals

An ASCII, GitSavvy like interface to Package Control.

Usually I browse packagecontrol.io or Github or the package control channel
for its pull requests.  If I see an interesting package, I don't want to
look that up again.  I have a web-url in the browser I can grab.

So, let's just paste that into the PCX dashboard to install it.

I want to install packages that are not (yet) registered.  E.g. to test them
while reviewing.  So, a git based install is required.

I want to quickly test out pull requests for packages.  I want to downgrade
if a version breaks my workflow.  I want to switch to a checked out (unpacked)
version to fix a bug myself.

Unpacking a package should configure the remotes.
Bonus point:  It would be nice to open such an unpacked package in a new
window. Using GitSavvy I could then create a fork, or add a fork and check
that out.

For abandoned packages, I want to switch to a fork (without unpacking) before
the registry is updated.  Maybe that never happens anyway.

If I get a notification from Github about a new release, I don't want to wait
for 3 hours.  I want to update immediately.

Ideally, release notes from Github could be used in addition to "messages.json".
These notes can be edited so I can fix my typos without making a new release.



