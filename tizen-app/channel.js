/* global TizenInputSources */
// Samsung's multiscreen channel on the display's local WebSocket server. Clients such as
// Home Assistant join the same channel and send source events to this app as host.
window.TizenInputChannel = (function () {
    "use strict";
    var sources = TizenInputSources;
    var channelUrl = "ws://127.0.0.1:8001/api/v2/channels/tizeninput.control?name=" + btoa("Tizen Input");
    var pollMilliseconds = 2000;
    var settleMilliseconds = 3000;
    var reconnectMilliseconds = 5000;
    var hooks = { record: function () {}, onState: function () {}, onList: function () {}, onConnection: function () {} };
    var channel = null;
    var lastState;
    var lastList;
    var listReaders = null;
    var listFailing = false;

    function broadcast(event, data) {
        if (channel && channel.readyState === WebSocket.OPEN) {
            channel.send(JSON.stringify({ method: "ms.channel.emit", params: { event: event, data: data, to: "broadcast" } }));
        }
    }

    function publishState(state, reason) {
        lastState = JSON.stringify(state);
        hooks.record("Input state (" + reason + ")", state);
        hooks.onState(state);
        broadcast("source.state", state);
    }

    function publishIfChanged(reason) {
        var state = sources.current();
        if (JSON.stringify(state) !== lastState) {
            publishState(state, reason);
        }
    }

    // Each selection gets a source.state reply once the input settles, even when unchanged,
    // because some inputs never call back.
    function select(target) {
        if (!target || typeof target.type !== "string" || typeof target.number !== "number") {
            hooks.record("Ignored source.set, expected {type, number}", target);
            return;
        }
        hooks.record("Selecting", { type: target.type, number: target.number });
        var settle = window.setTimeout(function () { publishState(sources.current(), "set"); }, settleMilliseconds);
        sources.select(target, function () {
            publishIfChanged("set");
        }, function (error) {
            hooks.record("Selecting failed", sources.describeError(error));
            window.clearTimeout(settle);
            publishState(sources.current(), "failed");
        });
    }

    function publishList(list) {
        var serialized = JSON.stringify(list);
        if (serialized !== lastList) {
            lastList = serialized;
            hooks.record("Input list", list);
            hooks.onList(list);
        }
        broadcast("source.list", list);
    }

    // Logged once per run of failures, since the poll would repeat it every 2 seconds.
    function listFailed(error) {
        listReaders = null;
        if (!listFailing) {
            hooks.record("Listing inputs failed", sources.describeError(error));
        }
        listFailing = true;
    }

    // Callers during a VIDEOSOURCE read share its result instead of starting another.
    function readList(onList) {
        if (listReaders) {
            listReaders.push(onList);
            return;
        }
        listReaders = [onList];
        try {
            sources.list(function (list) {
                var readers = listReaders;
                listReaders = null;
                listFailing = false;
                readers.forEach(function (reader) { reader(list); });
            }, listFailed);
        } catch (error) {
            listFailed(error);
        }
    }

    // VIDEOSOURCE marks an input connected while it has a signal, so a source device going to
    // sleep changes the list. Polls skip while a read is still running.
    function publishListIfChanged() {
        if (listReaders) {
            return;
        }
        readList(function (list) {
            if (JSON.stringify(list) !== lastList) {
                publishList(list);
            }
        });
    }

    var handlers = {
        "source.set": select,
        "source.get": function () { publishState(sources.current(), "get"); },
        "source.list": function () { readList(publishList); }
    };

    function receive(message) {
        var payload;
        try {
            payload = JSON.parse(message.data);
        } catch (error) {
            hooks.record("Channel message is not JSON");
            return;
        }
        var handler = handlers[payload.event];
        if (handler) {
            handler(payload.data);
        } else if (payload.event && payload.event.indexOf("ms.") !== 0) {
            hooks.record("Unknown channel event", payload.event);
        }
    }

    function connect() {
        try {
            channel = new WebSocket(channelUrl);
        } catch (error) {
            hooks.record("Channel connection failed", sources.describeError(error));
            return;
        }
        channel.onopen = function () {
            hooks.record("Channel open");
            hooks.onConnection(true);
        };
        channel.onmessage = receive;
        channel.onclose = function (event) {
            hooks.record("Channel closed", { code: event.code });
            hooks.onConnection(false);
            window.setTimeout(connect, reconnectMilliseconds);
        };
    }

    return {
        select: select,
        refreshList: publishListIfChanged,
        start: function (startHooks) {
            hooks = startHooks;
            publishState(sources.current(), "start");
            publishListIfChanged();
            // Tizen 6.5 has no source change listener, so a poll catches changes made elsewhere.
            window.setInterval(function () {
                publishIfChanged("poll");
                publishListIfChanged();
            }, pollMilliseconds);
            connect();
        }
    };
}());
