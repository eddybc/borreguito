
# pylint: disable=duplicate-code
# The modules will be merged for v12.0
import base64

from lxml import etree, objectify

from odoo import _, api, models
from odoo.tools.float_utils import float_is_zero
from odoo.exceptions import UserError

TYPE_CFDI22_TO_CFDI33 = {
    'ingreso': 'I',
    'egreso': 'E',
    'traslado': 'T',
    'nomina': 'N',
    'pago': 'P',
}


class AttachXmlsWizard(models.TransientModel):
    _inherit = 'attach.xmls.wizard'

    @staticmethod
    def _xml2capitalize(xml):
        """Receive 1 lxml etree object and change all attrib to Capitalize.
        """
        def recursive_lxml(element):
            for attrib, value in element.attrib.items():
                new_attrib = "%s%s" % (attrib[0].upper(), attrib[1:])
                element.attrib.update({new_attrib: value})

            for child in element.getchildren():
                child = recursive_lxml(child)
            return element
        return recursive_lxml(xml)

    @staticmethod
    def _l10n_mx_edi_convert_cfdi32_to_cfdi33(xml):
        """Convert a xml from cfdi32 to cfdi33
        :param xml: The xml 32 in lxml.objectify object
        :return: A xml 33 in lxml.objectify object
        """
        if xml.get('version', None) != '3.2':
            return xml
        # TODO: Process negative taxes "Retenciones" node
        # TODO: Process payment term
        xml = AttachXmlsWizard._xml2capitalize(xml)
        xml.attrib.update({
            'TipoDeComprobante': TYPE_CFDI22_TO_CFDI33[
                xml.attrib['TipoDeComprobante']],
            'Version': '3.3',
            # By default creates Payment Complement since that the imported
            # invoices are most imported for this propose if it is not the case
            # then modified manually from odoo.
            'MetodoPago': 'PPD',
        })
        return xml

    def get_impuestos(self, xml):
        if self._context.get('l10n_mx_edi_invoice_type') != 'out':
            return super().get_impuestos(xml)
        if not hasattr(xml, 'Impuestos'):
            return {}
        taxes_list = {'wrong_taxes': [], 'taxes_ids': {}, 'withno_account': []}
        taxes = []
        tax_tag_obj = self.env['account.account.tag']
        tax_obj = self.env['account.tax']
        for index, rec in enumerate(xml.Conceptos.Concepto):
            if not hasattr(rec, 'Impuestos'):
                continue
            taxes_list['taxes_ids'][index] = []
            taxes_xml = rec.Impuestos
            if hasattr(taxes_xml, 'Traslados'):
                taxes = self.collect_taxes(taxes_xml.Traslados.Traslado)
            if hasattr(taxes_xml, 'Retenciones'):
                taxes += self.collect_taxes(taxes_xml.Retenciones.Retencion)

            for tax in taxes:
                tax_tag_id = tax_tag_obj.search(
                    [('name', 'ilike', tax['tax'])])
                domain = [('type_tax_use', '=', 'sale'),
                          ('amount', '=', tax['rate'])]

                name = '%s(%s%%)' % (tax['tax'], tax['rate'])

                taxes_get = tax_obj.search(domain)
                tax_get = False
                for tax_id in taxes_get:
                    if not set(tax_id.tag_ids.ids).isdisjoint(tax_tag_id.ids):
                        tax_get = tax_id
                        break

                if not tax_tag_id or not tax_get:
                    taxes_list['wrong_taxes'].append(name)
                else:
                    if not tax_get.account_id.id:
                        taxes_list['withno_account'].append(
                            name if name else tax['tax'])
                    else:
                        tax['id'] = tax_get.id
                        tax['account'] = tax_get.account_id.id
                        tax['name'] = name if name else tax['tax']
                        taxes_list['taxes_ids'][index].append(tax)
        return taxes_list

    @api.model
    def check_xml(self, files, account_id=False):
        """ Validate that attributes in the xml before create invoice
        or attach xml in it
        :param files: dictionary of CFDIs in b64
        :type files: dict
        :param account_id: The account by default that must be used in the
            lines of the invoice if this is created
        :type account_id: int
        :return: the Result of the CFDI validation
        :rtype: dict
        """
        if self._context.get('l10n_mx_edi_invoice_type') != 'out':
            return super().check_xml(files, account_id=account_id)
        if not isinstance(files, dict):
            raise UserError(_("Something went wrong. The parameter for XML "
                              "files must be a dictionary."))
        inv_obj = self.env['account.invoice']
        partner_obj = self.env['res.partner']
        currency_obj = self.env['res.currency']
        wrongfiles = {}
        invoices = {}
        for key, xml64 in files.items():
            inv = inv_obj
            inv_id = False
            try:
                if isinstance(xml64, bytes):
                    xml64 = xml64.decode()
                xml_str = base64.b64decode(xml64.replace(
                    'data:text/xml;base64,', ''))
                # Fix the CFDIs emitted by the SAT
                xml_str = xml_str.replace(
                    b'xmlns:schemaLocation', b'xsi:schemaLocation')
                xml = objectify.fromstring(xml_str)
            except (AttributeError, SyntaxError) as exce:
                wrongfiles.update({key: {
                    'xml64': xml64, 'where': 'CheckXML',
                    'error': [exce.__class__.__name__, str(exce)]}})
                continue
            xml = self._l10n_mx_edi_convert_cfdi32_to_cfdi33(xml)
            if xml.get('TipoDeComprobante', False) != 'I':
                wrongfiles.update({key: {'cfdi_type': True, 'xml64': xml64}})
                continue
            xml_vat_emitter = xml.Emisor.get('Rfc', '').upper()
            xml_vat_receiver = xml.Receptor.get('Rfc', '').upper()
            xml_amount = xml.get('Total', 0.0)
            xml_tfd = inv_obj.l10n_mx_edi_get_tfd_etree(xml)
            xml_uuid = False if xml_tfd is None else xml_tfd.get(
                'UUID', '')
            xml_folio = xml.get('Folio', '')
            xml_currency = xml.get('Moneda', 'MXN')
            xml_taxes = self.get_impuestos(xml)
            version = xml.get('Version', xml.get('version'))
            xml_name_supplier = xml.Receptor.get('Nombre', '')
            domain = ['&', ('vat', '=', xml_vat_receiver)] if (
                xml_vat_receiver not in ['XEXX010101000', 'XAXX010101000']
            ) else ['&', ('name', '=ilike', xml_name_supplier)]
            domain.extend(['|',
                           ('supplier', '=', True), ('customer', '=', True)])
            exist_supplier = partner_obj.search(domain, limit=1)
            exist_reference = xml_folio and inv_obj.search(
                [('origin', '=', xml_folio),
                 ('type', '=', 'out_invoice'),
                 ('partner_id', '=', exist_supplier.id)], limit=1)
            if exist_reference and not exist_reference.l10n_mx_edi_cfdi_uuid:
                inv = exist_reference
                inv_id = inv.id
                exist_reference = False
                inv.l10n_mx_edi_update_sat_status()
            xml_status = inv.l10n_mx_edi_sat_status
            inv_vat_emitter = (
                self.env.user.company_id.vat or '').upper()
            inv_vat_receiver = (
                inv and inv.commercial_partner_id.vat or '').upper()
            inv_amount = inv.amount_total
            inv_folio = inv.origin
            uuid_dupli = xml_uuid in inv_obj.search([]).mapped(
                'l10n_mx_edi_cfdi_uuid')
            mxns = ['mxp', 'mxn', 'pesos', 'peso mexicano', 'pesos mexicanos']
            xml_currency = 'MXN' if xml_currency.lower(
            ) in mxns else xml_currency

            exist_currency = currency_obj.search(
                [('name', '=', xml_currency)], limit=1)

            errors = [
                (not xml_uuid, {'signed': True}),
                (xml_status == 'cancelled', {'cancel': True}),
                ((xml_uuid and uuid_dupli), {'uuid_duplicate': xml_uuid}),
                ((inv_id and inv_vat_receiver != xml_vat_receiver),
                 {'rfc_supplier': (xml_vat_receiver, inv_vat_receiver)}),
                ((not inv_id and exist_reference),
                 {'reference': (xml_name_supplier, xml_folio)}),
                (version != '3.3', {'version': True}),
                ((not inv_id and not exist_supplier),
                 {'customer': xml_name_supplier}),
                ((not inv_id and xml_currency and not exist_currency),
                 {'currency': xml_currency}),
                ((not inv_id and xml_taxes.get('wrong_taxes', False)),
                 {'taxes': xml_taxes.get('wrong_taxes', False)}),
                ((not inv_id and xml_taxes.get('withno_account', False)),
                 {'taxes_wn_accounts': xml_taxes.get(
                     'withno_account', False)}),
                ((inv_id and inv_folio != xml_folio),
                 {'folio': (xml_folio, inv_folio)}),
                ((inv_vat_emitter != xml_vat_emitter), {
                    'rfc_cust': (xml_vat_emitter, inv_vat_emitter)}),
                ((inv_id and not float_is_zero(float(inv_amount)-float(
                    xml_amount), precision_digits=2)), {
                        'amount': (xml_amount, inv_amount)})
            ]
            msg = {}
            for error in errors:
                if error[0]:
                    msg.update(error[1])
            if msg:
                msg.update({'xml64': xml64})
                wrongfiles.update({key: msg})
                continue

            if not inv_id:
                invoice_status = self.create_invoice(
                    xml, exist_supplier, exist_currency,
                    xml_taxes.get('taxes_ids', {}), account_id)

                if invoice_status['key'] is False:
                    del invoice_status['key']
                    invoice_status.update({'xml64': xml64})
                    wrongfiles.update({key: invoice_status})
                    continue

                del invoice_status['key']
                invoices.update({key: invoice_status})
                continue

            inv.l10n_mx_edi_cfdi = xml_str.decode('UTF-8')
            inv.generate_xml_attachment()
            inv.action_invoice_open()
            invoices.update({key: {'invoice_id': inv.id}})
        return {'wrongfiles': wrongfiles,
                'invoices': invoices}

    @api.multi
    def create_invoice(
            self, xml, supplier, currency_id, taxes, account_id=False):
        """ Create supplier invoice from xml file
        :param xml: xml file with the invoice data
        :type xml: etree
        :param supplier: customer partner
        :type supplier: res.partner
        :param currency_id: payment currency of the invoice
        :type currency_id: res.currency
        :param taxes: Datas of taxes
        :type taxes: list
        :param account_id: The account by default that must be used in the
            lines, if this is defined will to use this.
        :type account_id: int
        :return: the Result of the invoice creation
        :rtype: dict
        """
        if self._context.get('l10n_mx_edi_invoice_type') != 'out':
            return super().create_invoice(
                xml, supplier, currency_id, taxes, account_id=account_id)
        inv_obj = self.env['account.invoice']
        line_obj = self.env['account.invoice.line']
        journal = inv_obj.with_context(type='out_invoice')._default_journal()
        prod_obj = self.env['product.product']
        sat_code_obj = self.env['l10n_mx_edi.product.sat.code']
        uom_obj = uom_obj = self.env['product.uom']
        account_id = account_id or line_obj.with_context({
            'journal_id': journal.id,
            'type': 'out_invoice'})._default_account()
        invoice_line_ids = []
        msg = (_('Some products are not found in the system, and the account '
                 'that is used like default is not configured in the journal, '
                 'please set default account in the journal '
                 '%s to create the invoice.') % journal.name)

        date_inv = xml.get('Fecha', '').split('T')

        for index, rec in enumerate(xml.Conceptos.Concepto):
            name = rec.get('Descripcion', '')
            no_id = rec.get('NoIdentificacion', name)
            product_code = rec.get('ClaveProdServ', '')
            uom = rec.get('Unidad', '')
            uom_code = rec.get('ClaveUnidad', '')
            qty = rec.get('Cantidad', '')
            price = rec.get('ValorUnitario', '')
            amount = float(rec.get('Importe', '0.0'))
            sat_prod_code = sat_code_obj.search([
                ('code', '=', product_code)], limit=1)
            product_id = prod_obj.search([
                '|', '|', ('default_code', '=ilike', no_id),
                ('name', '=ilike', name),
                ('l10n_mx_edi_code_sat_id', '=', sat_prod_code.id)], limit=1)
            account_id = (
                product_id.property_account_income_id.id or product_id.
                categ_id.property_account_income_categ_id.id or account_id)

            if not account_id:
                return {
                    'key': False, 'where': 'CreateInvoice',
                    'error': [
                        _('Account to set in the lines not found.<br/>'), msg]}

            discount = 0.0
            if rec.get('Descuento') and amount:
                discount = (float(rec.get('Descuento', '0.0')) / amount) * 100

            domain_uom = [('name', '=ilike', uom)]
            line_taxes = [tax['id'] for tax in taxes.get(index, [])]
            code_sat = sat_code_obj.search([('code', '=', uom_code)], limit=1)
            domain_uom = [('l10n_mx_edi_code_sat_id', '=', code_sat.id)]
            uom_id = uom_obj.with_context(
                lang='es_MX').search(domain_uom, limit=1)

            if product_code in self._get_fuel_codes():
                tax = taxes.get(index)[0] if taxes.get(index, []) else {}
                qty = 1.0
                price = tax.get('amount') / (tax.get('rate') / 100)
                invoice_line_ids.append((0, 0, {
                    'account_id': account_id,
                    'name': _('FUEL - IEPS'),
                    'quantity': qty,
                    'uom_id': uom_id.id,
                    'price_unit': float(rec.get('Importe', 0)) - price,
                }))
            invoice_line_ids.append((0, 0, {
                'product_id': product_id.id,
                'account_id': account_id,
                'name': name,
                'quantity': float(qty),
                'uom_id': uom_id.id,
                'invoice_line_tax_ids': [(6, 0, line_taxes)],
                'price_unit': float(price),
                'discount': discount,
            }))

        xml_str = etree.tostring(xml, pretty_print=True, encoding='UTF-8')
        payment_method_id = self.env['l10n_mx_edi.payment.method'].search(
            [('code', '=', xml.get('FormaPago', '99'))], limit=1)
        payment_term = xml.get('MetodoPago') or False
        payment_condition = xml.get('CondicionesDePago') or False
        acc_pay_term = self.env['account.payment.term']
        if payment_term and payment_condition:
            acc_pay_term = acc_pay_term.search([
                ('name', '=', payment_condition)], limit=1)
        if payment_term and payment_term == 'PPD' and not acc_pay_term:
            acc_pay_term = self.env.ref(
                'l10n_mx_edi_customer_bills.aux_account_payment_term_ppd')
        invoice_id = inv_obj.create({
            'partner_id': supplier.id,
            'origin': xml.get('Folio', ''),
            'payment_term_id': acc_pay_term.id,
            'l10n_mx_edi_payment_method_id': payment_method_id.id,
            'l10n_mx_edi_usage': xml.Receptor.get('UsoCFDI', 'P01'),
            'date_invoice': date_inv[0],
            'currency_id': (
                currency_id.id or self.env.user.company_id.currency_id.id),
            'invoice_line_ids': invoice_line_ids,
            'type': 'out_invoice',
            'l10n_mx_edi_time_invoice': date_inv[1],
            'journal_id': journal.id,
        })
        if xml.get('version') == '3.2':
            # Global tax used for each line since that a manual tax line
            # won't have base amount assigned.
            tax_path = '//cfdi:Impuestos/cfdi:Traslados/cfdi:Traslado'
            tax_obj = self.env['account.tax']
            tax_domain = [('type_tax_use', '=', 'sale'),
                          ('company_id', '=', self.env.user.company_id.id)]
            for global_tax in xml.xpath(tax_path, namespaces=xml.nsmap):
                tax_name = global_tax.attrib.get('impuesto')
                tax_percent = float(global_tax.attrib.get('tasa'))
                tax_domain.extend([
                    ('amount_type', '=', 'percent'),
                    ('amount', '=', tax_percent),
                    ('tag_ids.name', '=', tax_name),
                ])
                tax = tax_obj.search(tax_domain, limit=1)
                invoice_id.invoice_line_ids.write({
                    'invoice_line_tax_ids': [(4, tax.id)]})

            # Global discount used for each line
            # Decimal rounding wrong values could be imported will fix manually
            discount_amount = float(xml.attrib.get('Descuento', 0))
            sub_total_amount = float(xml.attrib.get('subTotal', 0))
            if discount_amount and sub_total_amount:
                percent = discount_amount * 100 / sub_total_amount
                invoice_id.invoice_line_ids.write({'discount': percent})

        invoice_id.l10n_mx_edi_cfdi = xml_str.decode('UTF-8')
        invoice_id.generate_xml_attachment()
        invoice_id.compute_taxes()
        return {'key': True, 'invoice_id': invoice_id.id}

    @api.model
    def create_partner(self, xml64, key):
        """ It creates the supplier dictionary, getting data from the XML
        Receives an xml decode to read and returns a dictionary with data """
        # Default Mexico because only in Mexico are emitted CFDIs
        if self._context.get('l10n_mx_edi_invoice_type') != 'out':
            return super().create_partner(xml64, key)
        try:
            if isinstance(xml64, bytes):
                xml64 = xml64.decode()
            xml_str = base64.b64decode(xml64.replace(
                'data:text/xml;base64,', ''))
            # Fix the CFDIs emitted by the SAT
            xml_str = xml_str.replace(
                b'xmlns:schemaLocation', b'xsi:schemaLocation')
            xml = objectify.fromstring(xml_str)
        except BaseException as exce:
            return {
                key: False, 'xml64': xml64, 'where': 'CreatePartner',
                'error': [exce.__class__.__name__, str(exce)]}

        xml = self._l10n_mx_edi_convert_cfdi32_to_cfdi33(xml)
        rfc_receiver = xml.Receptor.get('Rfc', False)
        name = xml.Receptor.get('Nombre', rfc_receiver)

        # check if the partner exist from a previos invoice creation
        domain = [('vat', '=', rfc_receiver)] if rfc_receiver not in [
            'XEXX010101000', 'XAXX010101000'] else [('name', '=', name)]
        partner = self.env['res.partner'].search(domain, limit=1)
        if partner:
            return partner

        partner = self.env['res.partner'].create({
            'name': name,
            'company_type': 'company',
            'vat': rfc_receiver,
            'country_id': self.env.ref('base.mx').id,
            'supplier': False,
            'customer': True,
        })
        reference_xml = xml.get('Folio', '')
        msg = _('This partner was created when invoice %s was added from '
                'a XML file. Please verify that the datas of partner are '
                'correct.') % reference_xml
        partner.message_post(subject=_('Info'), body=msg)
        return partner
