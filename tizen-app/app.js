/* global tizen, TizenInputSources, TizenInputChannel */
(function () {
    "use strict";
    var sourceList = document.getElementById("sources");
    var channelStatus = document.getElementById("channel");
    var inputStatus = document.getElementById("input");
    var log = document.getElementById("log");
    var available = Boolean(window.tizen && tizen.systeminfo && tizen.tvwindow && tizen.application);

    function record(message, value) {
        var line = new Date().toISOString() + " " + message;
        if (value !== undefined) {
            line += " " + JSON.stringify(value);
        }
        console.log(line);
        log.textContent = (log.textContent + line + "\n").split("\n").slice(-80).join("\n");
        log.scrollTop = log.scrollHeight;
    }

    function enabledButtons() {
        return Array.prototype.filter.call(document.querySelectorAll("button"), function (button) {
            return !button.disabled;
        });
    }

    // Rebuilding the buttons drops focus, so it returns to the same input, or the first one at startup.
    function showSources(list) {
        var focused = sourceList.contains(document.activeElement) ? document.activeElement.value : null;
        sourceList.textContent = "";
        list.forEach(function (source) {
            var button = document.createElement("button");
            button.value = source.type + " " + source.number;
            button.textContent = TizenInputSources.label(source) + (source.connected ? "" : " (disconnected)");
            button.addEventListener("click", function () { TizenInputChannel.select(source); });
            sourceList.appendChild(button);
            if (button.value === focused) {
                button.focus();
            }
        });
        if (document.activeElement === document.body && sourceList.firstChild) {
            sourceList.firstChild.focus();
        }
    }

    function showState(state) {
        inputStatus.textContent = state ? TizenInputSources.label(state) : "Not identifiable through the Tizen API";
    }

    function showConnection(open) {
        channelStatus.textContent = open ? "Connected" : "Reconnecting";
    }

    function hide() {
        try {
            tizen.application.getCurrentApplication().hide();
        } catch (error) {
            record("Hiding failed", TizenInputSources.describeError(error));
        }
    }

    function exit() {
        tizen.application.getCurrentApplication().exit();
    }

    document.getElementById("refresh").addEventListener("click", TizenInputChannel.refreshList);
    document.getElementById("hide").addEventListener("click", hide);
    document.getElementById("exit").addEventListener("click", exit);
    document.addEventListener("visibilitychange", function () {
        record(document.hidden ? "App hidden" : "App visible");
    });
    // Return hides rather than exits, so the channel stays available.
    document.addEventListener("keydown", function (event) {
        if (event.keyCode === 10009 || event.key === "Escape") {
            event.preventDefault();
            hide();
            return;
        }
        var buttons = enabledButtons();
        var index = buttons.indexOf(document.activeElement);
        if (event.keyCode >= 37 && event.keyCode <= 40 && buttons.length) {
            event.preventDefault();
            var direction = event.keyCode === 37 || event.keyCode === 38 ? -1 : 1;
            buttons[(index + direction + buttons.length) % buttons.length].focus();
        } else if (event.keyCode === 13 && index >= 0) {
            event.preventDefault();
            buttons[index].click();
        }
    });

    if (available) {
        TizenInputChannel.start({ record: record, onState: showState, onList: showSources, onConnection: showConnection });
    } else {
        channelStatus.textContent = "Unavailable outside a Samsung Tizen display";
        Array.prototype.forEach.call(document.querySelectorAll("button"), function (button) {
            button.disabled = true;
        });
    }
}());
