// Browser tests for the Tizen app with mocked Tizen APIs and a mocked channel socket.
// They can't prove firmware behaviour, which differs between display models.
const assert = require("node:assert/strict");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { chromium } = require("playwright");

const location = pathToFileURL(path.resolve(__dirname, "../tizen-app/index.html")).href;

function mockTizen() {
    const videoSources = {
        connected: [{ type: "HDMI", number: 1 }, { type: "DP", number: 1, editName: "PC" }],
        disconnected: [{ type: "HDMI", number: 2 }]
    };
    let current = videoSources.connected[0];
    window.calls = [];
    window.unreadable = false;
    window.selectFails = false;
    window.sockets = [];
    window.reads = 0;
    window.readsFail = false;
    // While this is an array, VIDEOSOURCE reads wait in it for the test to answer them.
    window.heldReads = null;
    window.disconnect = (type, number) => {
        const source = videoSources.connected.find(candidate => candidate.type === type && candidate.number === number);
        videoSources.connected = videoSources.connected.filter(candidate => candidate !== source);
        videoSources.disconnected.push(source);
    };
    window.WebSocket = class {
        constructor(url) {
            this.url = url;
            this.readyState = 1;
            this.sent = [];
            window.sockets.push(this);
        }
        send(data) { this.sent.push(JSON.parse(data).params); }
    };
    window.WebSocket.OPEN = 1;
    window.tizen = {
        systeminfo: {
            getPropertyValue: (property, success, failure) => {
                const answer = () => {
                    if (window.readsFail) {
                        failure({ name: "UnknownError", message: "Unavailable" });
                        return;
                    }
                    success({ connected: videoSources.connected, disconnected: videoSources.disconnected });
                };
                window.reads += 1;
                if (window.heldReads) {
                    window.heldReads.push(answer);
                } else {
                    answer();
                }
            }
        },
        tvwindow: {
            getSource: () => {
                if (window.unreadable) {
                    throw { name: "UnknownError", message: "Unknown source type" };
                }
                return current;
            },
            setSource: (source, success, failure, type) => {
                window.calls.push(["setSource", source.type, source.number, type]);
                if (window.selectFails) {
                    failure({ name: "UnknownError", message: "Rejected" });
                    return;
                }
                current = source;
                success(source);
            }
        },
        application: {
            launch: (id, success) => {
                window.calls.push(["launch", id]);
                success();
            },
            getCurrentApplication: () => ({
                hide: () => window.calls.push("hide"),
                exit: () => window.calls.push("exit")
            })
        }
    };
}

async function testPreview(browser) {
    const page = await browser.newPage();
    await page.goto(location);
    assert.match(await page.locator("#channel").innerText(), /Unavailable/);
    assert.equal(await page.locator("button:enabled").count(), 0);
    await page.close();
}

async function openDisplay(browser) {
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.clock.install();
    await page.addInitScript(mockTizen);
    await page.goto(location);

    const sent = () => page.evaluate(() => window.sockets[0].sent);
    return {
        page,
        errors,
        sent,
        lastSent: async () => (await sent()).at(-1),
        calls: () => page.evaluate(() => window.calls),
        deliver: (event, data) => page.evaluate(([name, value]) => {
            window.sockets[0].onmessage({ data: JSON.stringify({ event: name, data: value }) });
        }, [event, data])
    };
}

async function testDisplay(browser) {
    const { page, errors, sent, lastSent, calls, deliver } = await openDisplay(browser);

    assert.equal(await page.evaluate(() => window.sockets[0].url),
        "ws://127.0.0.1:8001/api/v2/channels/tizeninput.control?name=" + Buffer.from("Tizen Input").toString("base64"));
    await page.evaluate(() => window.sockets[0].onopen());
    assert.equal(await page.locator("#channel").innerText(), "Connected");
    assert.equal(await page.locator("#input").innerText(), "HDMI 1");
    assert.deepEqual(await page.locator("#sources button").allInnerTexts(), ["PC (DP 1)", "HDMI 1", "HDMI 2 (disconnected)"]);

    await deliver("source.list");
    assert.deepEqual(await lastSent(), { event: "source.list", to: "broadcast", data: [
        { type: "DP", number: 1, name: "PC", connected: true },
        { type: "HDMI", number: 1, name: null, connected: true },
        { type: "HDMI", number: 2, name: null, connected: false }
    ] });

    await deliver("source.set", "DP 1");
    assert.equal((await sent()).length, 1);
    await deliver("source.set", { type: "HDMI", number: 9 });
    assert.deepEqual(await lastSent(), { event: "source.state", to: "broadcast", data: { type: "HDMI", number: 1 } });
    assert.equal((await calls()).length, 0);

    await deliver("source.set", { type: "DP", number: 1 });
    assert.deepEqual(await calls(), [["setSource", "DP", 1, "MAIN"], ["launch", "org.tizen.tv-viewer"]]);
    assert.deepEqual(await lastSent(), { event: "source.state", to: "broadcast", data: { type: "DP", number: 1 } });
    assert.equal(await page.locator("#input").innerText(), "DP 1");
    const afterSet = (await sent()).length;
    await page.clock.runFor(3000);
    assert.equal((await sent()).length, afterSet + 1);

    await deliver("source.get");
    assert.equal((await sent()).length, afterSet + 2);

    await page.evaluate(() => { window.unreadable = true; });
    await page.clock.runFor(2000);
    assert.deepEqual(await lastSent(), { event: "source.state", to: "broadcast", data: null });
    assert.match(await page.locator("#input").innerText(), /Not identifiable/);
    await page.clock.runFor(4000);
    assert.equal((await sent()).length, afterSet + 3);

    await page.evaluate(() => { window.unreadable = false; window.selectFails = true; });
    await page.getByRole("button", { name: "HDMI 1", exact: true }).click();
    assert.deepEqual((await calls()).slice(-2), [["setSource", "HDMI", 1, "MAIN"], ["launch", "org.tizen.tv-viewer"]]);
    assert.equal((await lastSent()).event, "source.state");

    await page.locator("#hide").click();
    await page.keyboard.press("Escape");
    await page.locator("#exit").click();
    assert.deepEqual((await calls()).slice(-3), ["hide", "hide", "exit"]);
    await page.evaluate(() => window.sockets[0].onclose({ code: 1006 }));
    assert.equal(await page.locator("#channel").innerText(), "Reconnecting");
    await page.clock.runFor(5000);
    assert.equal(await page.evaluate(() => window.sockets.length), 2);
    assert.deepEqual(errors, []);
    await page.close();
}

async function testInputList(browser) {
    const { page, errors, sent, deliver } = await openDisplay(browser);
    const listsSent = async () => (await sent()).filter(message => message.event === "source.list");
    const reads = () => page.evaluate(() => window.reads);
    await page.evaluate(() => window.sockets[0].onopen());
    await page.getByRole("button", { name: "PC (DP 1)" }).focus();

    await page.clock.runFor(4000);
    assert.deepEqual(await listsSent(), []);

    await page.evaluate(() => window.disconnect("DP", 1));
    await page.clock.runFor(4000);
    assert.deepEqual(await listsSent(), [{ event: "source.list", to: "broadcast", data: [
        { type: "DP", number: 1, name: "PC", connected: false },
        { type: "HDMI", number: 1, name: null, connected: true },
        { type: "HDMI", number: 2, name: null, connected: false }
    ] }]);
    assert.deepEqual(await page.locator("#sources button").allInnerTexts(),
        ["PC (DP 1) (disconnected)", "HDMI 1", "HDMI 2 (disconnected)"]);
    assert.equal(await page.evaluate(() => document.activeElement.textContent), "PC (DP 1) (disconnected)");

    // Polls and requests while a read is unanswered don't start another.
    await page.evaluate(() => { window.reads = 0; window.heldReads = []; });
    await page.clock.runFor(4000);
    await deliver("source.list");
    assert.equal(await reads(), 1);
    await page.evaluate(() => {
        window.heldReads.forEach(answer => answer());
        window.heldReads = null;
    });
    assert.equal((await listsSent()).length, 2);
    await page.clock.runFor(2000);
    assert.equal(await reads(), 2);
    assert.equal((await listsSent()).length, 2);

    // A run of failing reads logs once.
    const failuresLogged = async () => (await page.locator("#log").textContent()).split("Listing inputs failed").length - 1;
    await page.evaluate(() => { window.readsFail = true; });
    await page.clock.runFor(6000);
    assert.equal(await failuresLogged(), 1);
    await page.evaluate(() => { window.readsFail = false; });
    await page.clock.runFor(2000);
    await page.evaluate(() => { window.readsFail = true; });
    await page.clock.runFor(2000);
    assert.equal(await failuresLogged(), 2);
    assert.equal((await listsSent()).length, 2);
    assert.deepEqual(errors, []);
    await page.close();
}

(async function () {
    const browser = await chromium.launch({ headless: true });
    try {
        await testPreview(browser);
        await testDisplay(browser);
        await testInputList(browser);
        console.log("Passed preview, channel protocol, selection, polling, page controls, reconnect and input list tests.");
    } finally {
        await browser.close();
    }
}()).catch(error => {
    console.error(error);
    process.exitCode = 1;
});
