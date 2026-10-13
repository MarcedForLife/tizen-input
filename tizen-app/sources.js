/* global tizen */
// Tizen's video source APIs, exposed as plain {type, number} objects.
window.TizenInputSources = (function () {
    "use strict";
    var inputViewer = "org.tizen.tv-viewer";

    function describeError(error) {
        return { name: error.name, message: error.message };
    }

    function label(source) {
        var position = source.type + " " + source.number;
        return source.name ? source.name + " (" + position + ")" : position;
    }

    // getSource throws for inputs the API can't name, such as USB-C on the Odyssey G8.
    function current() {
        try {
            var source = tizen.tvwindow.getSource("MAIN");
            return { type: source.type, number: source.number, signal: source.signal };
        } catch (error) {
            return null;
        }
    }

    function readNative(onSources, onError) {
        tizen.systeminfo.getPropertyValue("VIDEOSOURCE", function (sources) {
            var connected = sources.connected || [];
            onSources(connected.concat(sources.disconnected || []), connected);
        }, onError);
    }

    // Sorted by position, so the order holds when an input gains or loses a signal.
    function list(onList, onError) {
        readNative(function (sources, connected) {
            onList(sources.map(function (source) {
                return {
                    type: source.type,
                    number: source.number,
                    name: source.editName || null,
                    connected: connected.indexOf(source) !== -1
                };
            }).sort(function (first, second) {
                return first.type.localeCompare(second.type) || first.number - second.number;
            }));
        }, onError);
    }

    // setSource only accepts the native objects from VIDEOSOURCE. Opening the input viewer
    // afterwards brings the selected input to the front, whatever was showing before.
    function select(target, onSelected, onError) {
        readNative(function (sources) {
            var source = sources.filter(function (candidate) {
                return candidate.type === target.type && candidate.number === target.number;
            })[0];
            if (!source) {
                onError({ name: "NotFoundError", message: "No source " + target.type + " " + target.number });
                return;
            }
            try {
                tizen.tvwindow.setSource(source, onSelected, onError, "MAIN");
                tizen.application.launch(inputViewer, function () {}, onError);
            } catch (error) {
                onError(error);
            }
        }, onError);
    }

    return { current: current, describeError: describeError, label: label, list: list, select: select };
}());
