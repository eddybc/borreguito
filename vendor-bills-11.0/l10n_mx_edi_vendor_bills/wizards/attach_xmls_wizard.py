# Copyright 2017, Jarsa Sistemas, S.A. de C.V.
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).

import base64

from lxml import etree, objectify

from odoo import _, api, fields, models
from odoo.tools.float_utils import float_is_zero
from odoo.tools import float_round
from odoo.exceptions import UserError


class AttachXmlsWizard(models.TransientModel):
    _name = 'attach.xmls.wizard'

    dragndrop = fields.Char()

    @staticmethod
    def collect_taxes(taxes_xml):
        """ Get tax data of the Impuesto node of the xml and return
        dictionary with taxes datas
        :param taxes_xml: Impuesto node of xml
        :type taxes_xml: etree
        :return: A list with the taxes data
        :rtype: list
        """
        taxes = []
        tax_codes = {'001': 'ISR', '002': 'IVA', '003': 'IEPS'}
        for rec in taxes_xml:
            tax_xml = rec.get('Impuesto', '')
            tax_xml = tax_codes.get(tax_xml, tax_xml)
            amount_xml = float(rec.get('Importe', '0.0'))
            rate_xml = float_round(
                float(rec.get('TasaOCuota', '0.0')) * 100, 4)
            if 'Retenciones' in rec.getparent().tag:
                tax_xml = tax_xml
                amount_xml = amount_xml * -1
                rate_xml = rate_xml * -1

            taxes.append({'rate': rate_xml, 'tax': tax_xml,
                          'amount': amount_xml})
        return taxes

    def get_impuestos(self, xml):
        if not hasattr(xml, 'Impuestos'):
            return {}
        taxes_list = {'wrong_taxes': [], 'taxes_ids': {}, 'withno_account': []}
        taxes = []
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
                tax_group_id = self.env['account.tax.group'].search(
                    [('name', 'ilike', tax['tax'])])
                domain = [('tax_group_id', 'in', tax_group_id.ids),
                          ('type_tax_use', '=', 'purchase'),
                          ('amount', '=', tax['rate'])]

                name = '%s(%s%%)' % (tax['tax'], tax['rate'])

                tax_get = self.env['account.tax'].search(domain, limit=1)

                if not tax_group_id or not tax_get:
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

    def validate_documents(self, key, xml, account_id):
        """ Validate the incoming or outcoming document before create or
        attach the xml to invoice
        :param key: Name of the document that is being validated
        :type key: str
        :param xml: xml file with the datas of purchase
        :type xml: etree
        :param account_id: The account by default that must be used in the
            lines of the invoice if this is created
        :type account_id: int
        :return: Result of the validation of the CFDI and the invoices created.
        :rtype: dict
        """
        wrongfiles = {}
        invoices = {}
        inv_obj = self.env['account.invoice']
        partner_obj = self.env['res.partner']
        currency_obj = self.env['res.currency']
        inv = inv_obj
        inv_id = False
        xml_str = etree.tostring(xml, pretty_print=True, encoding='UTF-8')

        xml_vat_emitter = xml.Emisor.get('Rfc', '').upper()
        xml_vat_receiver = xml.Receptor.get('Rfc', '').upper()
        xml_amount = xml.get('Total', 0.0)
        xml_tfd = inv_obj.l10n_mx_edi_get_tfd_etree(xml)
        xml_uuid = False if xml_tfd is None else xml_tfd.get('UUID', '')
        xml_folio = xml.get('Folio', '')
        xml_currency = xml.get('Moneda', 'MXN')
        xml_taxes = self.get_impuestos(xml)
        version = xml.get('Version', xml.get('version'))
        xml_name_supplier = xml.Emisor.get('Nombre', '')
        xml_type_of_document = xml.get('TipoDeComprobante', False)
        xml_related_uuid = related_invoice = False
        exist_supplier = partner_obj.search(
            ['&', ('vat', '=', xml_vat_emitter), '|',
             ('supplier', '=', True), ('customer', '=', True)], limit=1)
        invoice = xml_folio and inv_obj.search(
            [('reference', '=ilike', xml_folio),
                ('partner_id', '=', exist_supplier.id)], limit=1)
        exist_reference = invoice if invoice and xml_uuid != invoice.l10n_mx_edi_cfdi_uuid else False  # noqa
        if exist_reference and not exist_reference.l10n_mx_edi_cfdi_uuid:
            inv = exist_reference
            inv_id = inv.id
            exist_reference = False
            inv.l10n_mx_edi_update_sat_status()
        xml_status = inv.l10n_mx_edi_sat_status
        inv_vat_receiver = (
            self.env.user.company_id.vat or '').upper()
        inv_vat_emitter = (
            inv and inv.commercial_partner_id.vat or '').upper()
        inv_amount = inv.amount_total
        inv_folio = inv.reference
        domain = [('l10n_mx_edi_cfdi_name', '!=', False)]
        if xml_type_of_document == 'I':
            domain += [('type', '=', 'in_invoice')]
        if xml_type_of_document == 'E':
            domain += [('type', '=', 'in_refund')]
        uuid_dupli = xml_uuid in inv_obj.search(domain).mapped(
            'l10n_mx_edi_cfdi_uuid')
        mxns = ['mxp', 'mxn', 'pesos', 'peso mexicano', 'pesos mexicanos']
        xml_currency = 'MXN' if xml_currency.lower(
        ) in mxns else xml_currency

        exist_currency = currency_obj.search(
            [('name', '=', xml_currency)], limit=1)
        if xml_type_of_document == 'E':
            xml_related_uuid = xml.CfdiRelacionados.CfdiRelacionado.get('UUID')
            related_invoice = xml_related_uuid in inv_obj.search([
                ('l10n_mx_edi_cfdi_name', '!=', False),
                ('type', '=', 'in_invoice')]).mapped('l10n_mx_edi_cfdi_uuid')
        errors = [
            (not xml_uuid, {'signed': True}),
            (xml_status == 'cancelled', {'cancel': True}),
            ((xml_uuid and uuid_dupli), {'uuid_duplicate': xml_uuid}),
            ((inv_vat_receiver != xml_vat_receiver),
             {'rfc': (xml_vat_receiver, inv_vat_receiver)}),
            ((not inv_id and exist_reference),
             {'reference': (xml_name_supplier, xml_folio)}),
            (version != '3.3', {'version': True}),
            ((not inv_id and not exist_supplier),
             {'supplier': xml_name_supplier}),
            ((not inv_id and xml_currency and not exist_currency),
             {'currency': xml_currency}),
            ((not inv_id and xml_taxes.get('wrong_taxes', False)),
             {'taxes': xml_taxes.get('wrong_taxes', False)}),
            ((not inv_id and xml_taxes.get('withno_account', False)),
             {'taxes_wn_accounts': xml_taxes.get(
                 'withno_account', False)}),
            ((inv_id and inv_folio != xml_folio),
             {'folio': (xml_folio, inv_folio)}),
            ((inv_id and inv_vat_emitter != xml_vat_emitter), {
                'rfc_supplier': (xml_vat_emitter, inv_vat_emitter)}),
            ((inv_id and not float_is_zero(float(inv_amount)-float(
                xml_amount), precision_digits=2)), {
                    'amount': (xml_amount, inv_amount)}),
            ((xml_related_uuid and not related_invoice),
             {'invoice_not_found': xml_related_uuid})
        ]
        msg = {}
        for error in errors:
            if error[0]:
                msg.update(error[1])
        if msg:
            msg.update({'xml64': True})
            wrongfiles.update({key: msg})
            return {'wrongfiles': wrongfiles, 'invoices': invoices}

        if not inv_id:
            invoice_status = self.create_invoice(
                xml, exist_supplier, exist_currency,
                xml_taxes.get('taxes_ids', {}), account_id)

            if invoice_status['key'] is False:
                del invoice_status['key']
                invoice_status.update({'xml64': True})
                wrongfiles.update({key: invoice_status})
                return {'wrongfiles': wrongfiles, 'invoices': invoices}

            del invoice_status['key']
            invoices.update({key: invoice_status})
            return {'wrongfiles': wrongfiles, 'invoices': invoices}

        inv.l10n_mx_edi_cfdi = xml_str.decode('UTF-8')
        inv.generate_xml_attachment()
        inv.reference = '%s|%s' % (xml_folio, xml_uuid.split('-')[0])
        invoices.update({key: {'invoice_id': inv.id}})
        return {'wrongfiles': wrongfiles, 'invoices': invoices}

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
        if not isinstance(files, dict):
            raise UserError(_("Something went wrong. The parameter for XML "
                              "files must be a dictionary."))
        wrongfiles = {}
        invoices = {}
        outgoing_docs = {}
        for key, xml64 in files.items():
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
            if xml.get('TipoDeComprobante', False) == 'E':
                outgoing_docs.update({key: {'xml': xml, 'xml64': xml64}})
                continue
            elif xml.get('TipoDeComprobante', False) != 'I':
                wrongfiles.update({key: {'cfdi_type': True, 'xml64': xml64}})
                continue
            # Check the incoming documents
            validated_documents = self.validate_documents(key, xml, account_id)
            wrongfiles.update(validated_documents.get('wrongfiles'))
            if wrongfiles.get(key, False) and \
                    wrongfiles[key].get('xml64', False):
                wrongfiles[key]['xml64'] = xml64
            invoices.update(validated_documents.get('invoices'))
        # Check the outgoing documents
        for key, value in outgoing_docs.items():
            xml64 = value.get('xml64')
            xml = value.get('xml')
            validated_documents = self.validate_documents(key, xml, account_id)
            wrongfiles.update(validated_documents.get('wrongfiles'))
            if wrongfiles.get(key, False) and \
                    wrongfiles[key].get('xml64', False):
                wrongfiles[key]['xml64'] = xml64
            invoices.update(validated_documents.get('invoices'))
        return {'wrongfiles': wrongfiles,
                'invoices': invoices}

    @api.multi
    def create_invoice(
            self, xml, supplier, currency_id, taxes, account_id=False):
        """ Create supplier invoice from xml file
        :param xml: xml file with the datas of purchase
        :type xml: etree
        :param supplier: supplier partner
        :type supplier: res.partner
        :param currency_id: payment currency of the purchase
        :type currency_id: res.currency
        :param taxes: Datas of taxes
        :type taxes: list
        :param account_id: The account by default that must be used in the
            lines, if this is defined will to use this.
        :type account_id: int
        :return: the Result of the invoice creation
        :rtype: dict
        """
        inv_obj = self.env['account.invoice']
        line_obj = self.env['account.invoice.line']
        xml_type_doc = xml.get('TipoDeComprobante', False)
        type_invoice = 'in_invoice' if xml_type_doc == 'I' else 'in_refund'
        journal = inv_obj.with_context(type=type_invoice)._default_journal()
        prod_obj = self.env['product.product']
        sat_code_obj = self.env['l10n_mx_edi.product.sat.code']
        uom_obj = uom_obj = self.env['product.uom']
        account_id = account_id or line_obj.with_context({
            'journal_id': journal.id, 'type': 'in_invoice'})._default_account()
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
                product_id.property_account_expense_id.id or product_id.
                categ_id.property_account_expense_categ_id.id or account_id)

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
        xml_tfd = inv_obj.l10n_mx_edi_get_tfd_etree(xml)
        uuid = False if xml_tfd is None else xml_tfd.get('UUID', '')
        invoice_id = inv_obj.create({
            'partner_id': supplier.id,
            'reference': '%s|%s' % (xml.get('Folio', ''), uuid.split('-')[0]),
            'date_invoice': date_inv[0],
            'currency_id': (
                currency_id.id or self.env.user.company_id.currency_id.id),
            'invoice_line_ids': invoice_line_ids,
            'type': type_invoice,
            'l10n_mx_edi_time_invoice': date_inv[1],
            'journal_id': journal.id,
        })
        invoice_id.l10n_mx_edi_cfdi = xml_str.decode('UTF-8')
        invoice_id.generate_xml_attachment()
        if xml_type_doc == 'E':
            xml_related_uuid = xml.CfdiRelacionados.CfdiRelacionado.get('UUID')
            invoice_id._set_cfdi_origin('01', [xml_related_uuid])
            related_invoices = inv_obj.search([
                ('partner_id', '=', supplier.id), ('type', '=', 'in_invoice')])
            related_invoices = related_invoices.filtered(
                lambda inv: inv.l10n_mx_edi_cfdi_uuid == xml_related_uuid)
            related_invoices.refund_invoice_ids |= invoice_id
        return {'key': True, 'invoice_id': invoice_id.id}

    @api.model
    def create_partner(self, xml64, key):
        """ It creates the supplier dictionary, getting data from the XML
        Receives an xml decode to read and returns a dictionary with data """
        # Default Mexico because only in Mexico are emitted CFDIs
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

        reference_xml = xml.get('Folio', '')
        rfc_emitter = xml.Emisor.get('Rfc', False)
        name = xml.Emisor.get('Nombre', rfc_emitter)

        # check if the partner exist from a previos invoice creation
        partner = self.env['res.partner'].search([
            ('name', '=', name), ('vat', '=', rfc_emitter)])
        if partner:
            return partner

        partner = self.env['res.partner'].create({
            'name': name,
            'company_type': 'company',
            'vat': rfc_emitter,
            'country_id': self.env.ref('base.mx').id,
            'supplier': True,
            'customer': False,
        })
        msg = _('This partner was created when invoice %s was added from '
                'a XML file. Please verify that the datas of partner are '
                'correct.') % reference_xml
        partner.message_post(subject=_('Info'), body=msg)
        return partner

    @api.model
    def _get_fuel_codes(self):
        """Return the codes that could be used in FUEL"""
        return [str(r) for r in range(15101500, 15101513)]
