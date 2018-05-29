/*
 * View model for OctoPrint-3DPrinterOS
 *
 * Author: Maksym Kozlov
 * License: AGPLv3
 */

$(function() {
    function c3DPrinterOSViewModel(parameters) {
        var self = this;

        self.settingsModel = parameters[0];
        self.fileModel = parameters[1];
        self.registering = ko.observable(false);
        self.registrationFailed = ko.observable(false);
        self.registrationFailedReason = ko.observable("");
        self.printerTypes = ko.observableArray([]);
        self.printerRegistered = ko.observable(false);
        self.registrationCode = ko.observable('');
        self.registrationLink = ko.observable('');
        self.registeredEmail = ko.observable('');
        self.registerIndex = 0;

        var downloadLinkFunc = self.fileModel.downloadLink;
        self.fileModel.downloadLink = function (data) {
            if (data['origin']=='local' && data['path']=='3dprinteros/3dprinteros.gcode') {
                return false;
            }
            return downloadLinkFunc(data);
        };

        self.onBeforeBinding = function() {
            console.log('onBeforeBinding');
            self.settings = self.settingsModel.settings.plugins.c3dprinteros;
            self.printerTypes(JSON.parse(self.settings.printer_types_json()));
        };

        self.onSettingsShown = function() {
            console.log('onSettingsShown');
            self.printerRegistered(self.settings.registered());
        };

        self.showRegistration = function() {
            console.log('showRegistration', self.registering());
            if (self.registering()) return;
            self.registering(true);
            self.registrationCode('');
            self.registrationLink('');
            self.registeredEmail('');
            self.registerIndex++;
            self.checkRegistrationStatus(self.registerIndex);
            $("#plugin_c3dprinteros_registration").unbind('hidden').on('hidden', self.registerCancel)
                .modal("show");
        };

        self.unregister = function () {
            OctoPrint.simpleApiCommand("c3dprinteros", "unregister", {}).done(function(response) {
                console.log("unregister response" + JSON.stringify(response));
                self.printerRegistered(false);
            }).fail(function (res, res2) {
                console.log('fail1', res, res2);
            });
        };

        self.registerCancel = function() {
            self.registering(false);
            console.log('registerCancel', self.registering());
        };

        self.checkRegistrationStatus = function (index) {
            if (!self.registering() || index!=self.registerIndex) return;
            var obj = {'printer_type': self.settings.printer_type()};
            if (self.registrationCode()) {
                obj['code'] = self.registrationCode();
            }
            console.log("register status request ", obj);
            OctoPrint.simpleApiCommand("c3dprinteros", "register", obj).done(function(response) {
                self.registrationFailed(false);
                console.log("register status response ", response);
                if (response.auth_token) {
                    self.registeredEmail(response.email);
                    self.registering(false);
                    self.printerRegistered(true);
                } else {
                    if (response.code && response.code!=self.registrationCode()) {
                        self.registrationCode(response.code);
                        self.registrationLink('https://'+self.settings.url()+'/printing/');
                    }
                    setTimeout(function () { self.checkRegistrationStatus(index); }, 3000);
                }
            }).fail(function (res) {
                console.log('fail2', res);
                var msg = 'Some error from server';
                if (res.responseJSON && res.responseJSON.message) {
                    msg = res.responseJSON.message
                } else if (res.responseText) {
                    msg = 'Error from server: ' + res.responseText;
                }
                self.registrationFailedReason(msg);
                self.registrationFailed(true);
                setTimeout(function () { self.checkRegistrationStatus(index); }, 3000);
            });
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: c3DPrinterOSViewModel,
        dependencies: [ 'settingsViewModel', 'filesViewModel' ],
        elements: [ '#settings_plugin_3dprinteros' ]
    });
});
