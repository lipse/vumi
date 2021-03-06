# -*- test-case-name: vumi.transports.smpp.tests.test_mica -*-

from vumi.config import ConfigInt
from vumi.components.session import SessionManager
from vumi.message import TransportUserMessage
from vumi.transports.smpp.processors import default

from twisted.internet.defer import inlineCallbacks, returnValue


def make_vumi_session_identifier(msisdn, mica_session_identifier):
    return '%s+%s' % (msisdn, mica_session_identifier)


class DeliverShortMessageProcessorConfig(
        default.DeliverShortMessageProcessorConfig):

    max_session_length = ConfigInt(
        'Maximum length a USSD sessions data is to be kept for in seconds.',
        default=60 * 3, static=True)


class DeliverShortMessageProcessor(default.DeliverShortMessageProcessor):

    CONFIG_CLASS = DeliverShortMessageProcessorConfig

    def __init__(self, transport, config):
        super(DeliverShortMessageProcessor, self).__init__(transport, config)
        self.transport = transport
        self.redis = transport.redis
        self.config = self.CONFIG_CLASS(config, static=True)
        self.session_manager = SessionManager(
            self.redis, max_session_length=self.config.max_session_length)

    @inlineCallbacks
    def handle_deliver_sm_ussd(self, pdu, pdu_params, pdu_opts):
        service_op = pdu_opts['ussd_service_op']
        mica_session_identifier = pdu_opts['user_message_reference']
        vumi_session_identifier = make_vumi_session_identifier(
            pdu_params['source_addr'], mica_session_identifier)

        session_event = 'close'
        if service_op == '01':
            # PSSR request. Let's assume it means a new session.
            session_event = 'new'
            ussd_code = pdu_params['short_message']
            content = None

            yield self.session_manager.create_session(
                vumi_session_identifier, ussd_code=ussd_code)

        elif service_op == '17':
            # PSSR response. This means session end.
            session_event = 'close'

            session = yield self.session_manager.load_session(
                vumi_session_identifier)
            ussd_code = session['ussd_code']
            content = None

            yield self.session_manager.clear_session(vumi_session_identifier)

        else:
            session_event = 'continue'

            session = yield self.session_manager.load_session(
                vumi_session_identifier)
            ussd_code = session['ussd_code']
            content = pdu_params['short_message']

        # This is stashed on the message and available when replying
        # with a `submit_sm`
        session_info = {
            'session_identifier': mica_session_identifier,
        }

        decoded_msg = self.decode_message(content,
                                          pdu_params['data_coding'])

        result = yield self.handle_short_message_content(
            source_addr=pdu_params['source_addr'],
            destination_addr=ussd_code,
            short_message=decoded_msg,
            message_type='ussd',
            session_event=session_event,
            session_info=session_info)
        returnValue(result)


class SubmitShortMessageProcessorConfig(
        default.SubmitShortMessageProcessorConfig):

    max_session_length = ConfigInt(
        'Maximum length a USSD sessions data is to be kept for in seconds.',
        default=60 * 3, static=True)


class SubmitShortMessageProcessor(default.SubmitShortMessageProcessor):

    CONFIG_CLASS = SubmitShortMessageProcessorConfig

    def __init__(self, transport, config):
        super(SubmitShortMessageProcessor, self).__init__(transport, config)
        self.transport = transport
        self.redis = transport.redis
        self.config = self.CONFIG_CLASS(config, static=True)
        self.session_manager = SessionManager(
            self.redis, max_session_length=self.config.max_session_length)

    @inlineCallbacks
    def handle_outbound_message(self, message, protocol):
        to_addr = message['to_addr']
        from_addr = message['from_addr']
        text = message['content']

        session_event = message['session_event']
        transport_type = message['transport_type']
        optional_parameters = {}

        if transport_type == 'ussd':
            continue_session = (
                session_event != TransportUserMessage.SESSION_CLOSE)
            session_info = message['transport_metadata'].get(
                'session_info', {})
            mica_session_identifier = session_info.get(
                'session_identifier', '')
            vumi_session_identifier = make_vumi_session_identifier(
                to_addr, mica_session_identifier)

            optional_parameters.update({
                'ussd_service_op': ('02' if continue_session else '17'),
                'user_message_reference': (
                    str(mica_session_identifier).zfill(2)),
            })

            if not continue_session:
                yield self.session_manager.clear_session(
                    vumi_session_identifier)

        if self.config.send_long_messages:
            resp = yield protocol.submit_sm_long(
                to_addr.encode('ascii'),
                long_message=text.encode(self.config.submit_sm_encoding),
                data_coding=self.config.submit_sm_data_coding,
                source_addr=from_addr.encode('ascii'),
                optional_parameters=optional_parameters,
            )

        elif self.config.send_multipart_sar:
            resp = yield protocol.submit_csm_sar(
                to_addr.encode('ascii'),
                short_message=text.encode(self.config.submit_sm_encoding),
                data_coding=self.config.submit_sm_data_coding,
                source_addr=from_addr.encode('ascii'),
                optional_parameters=optional_parameters,
            )

        elif self.config.send_multipart_udh:
            resp = yield protocol.submit_csm_udh(
                to_addr.encode('ascii'),
                short_message=text.encode(self.config.submit_sm_encoding),
                data_coding=self.config.submit_sm_data_coding,
                source_addr=from_addr.encode('ascii'),
                optional_parameters=optional_parameters,
            )
        else:
            resp = yield protocol.submit_sm(
                to_addr.encode('ascii'),
                short_message=text.encode(self.config.submit_sm_encoding),
                data_coding=self.config.submit_sm_data_coding,
                source_addr=from_addr.encode('ascii'),
                optional_parameters=optional_parameters,
            )

        returnValue(resp)
