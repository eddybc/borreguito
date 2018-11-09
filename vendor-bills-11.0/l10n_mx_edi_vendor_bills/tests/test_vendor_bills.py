# See LICENSE file for full copyright and licensing details.

import base64
import os

from lxml import etree
from lxml.objectify import fromstring

from odoo.tests.common import TransactionCase
from odoo.tools import misc


class MxEdiVendorBills(TransactionCase):

    def setUp(self):
        super(MxEdiVendorBills, self).setUp()
        self.invoice_obj = self.env['account.invoice']
        self.attach_wizard_obj = self.env['attach.xmls.wizard']
        self.partner = self.env.ref('base.res_partner_1')
        self.product = self.env.ref('product.product_product_24')
        self.key = 'bill.xml'
        self.xml_str = misc.file_open(os.path.join(
            'l10n_mx_edi_vendor_bills', 'tests', self.key)
        ).read().encode('UTF-8')
        self.xml_tree = fromstring(self.xml_str)
        self.xml_fuel_str = misc.file_open(os.path.join(
            'l10n_mx_edi_vendor_bills', 'tests', 'fuel_bill.xml')
        ).read().encode('UTF-8')
        self.xml_fuel_tree = fromstring(self.xml_fuel_str)

    def test_001_create_vendor_bill(self):
        """Create a vendor bill from xml and check its values"""
        # create invoice
        res = self.attach_wizard_obj.check_xml({self.key: base64.b64encode(
            self.xml_str).decode('UTF-8')})
        invoices = res.get('invoices', {})
        inv_id = invoices.get(self.key, {}).get('invoice_id', False)
        self.assertTrue(inv_id, "Error: Invoice creation")
        # check values
        inv = self.invoice_obj.browse(inv_id)
        xml_amount = float(self.xml_tree.get('Total', 0.0))
        self.assertEqual(inv.amount_total, xml_amount, "Error: Total amount")
        xml_vat_emitter = self.xml_tree.Emisor.get('Rfc', '').upper()
        self.assertEqual(
            inv.partner_id.vat, xml_vat_emitter, "Error: Emitter")
        xml_vat_receiver = self.xml_tree.Receptor.get('Rfc', '').upper()
        self.assertEqual(self.env.user.company_id.vat, xml_vat_receiver,
                         "Error: Receiver")
        xml_tfd = self.invoice_obj.l10n_mx_edi_get_tfd_etree(self.xml_tree)
        uuid = False if xml_tfd is None else xml_tfd.get('UUID', '')
        xml_folio = '%s|%s' % (self.xml_tree.get('Folio', ''), uuid.split(
            '-')[0])
        self.assertEqual(inv.reference, xml_folio, "Error: Reference")

    def test_002_create_vendor_bill_from_partner_creation(self):
        """Create a vendor bill without a existing partner"""
        self.xml_tree.Emisor.set('Rfc', 'COPU930915KW7')
        self.xml_tree.Emisor.set('Nombre', 'USUARIO COMP PRUEBA')
        xml64 = base64.b64encode(etree.tostring(
            self.xml_tree, pretty_print=True, xml_declaration=True,
            encoding='UTF-8')).decode('UTF-8')
        self.attach_wizard_obj.create_partner(xml64, self.key)
        res = self.attach_wizard_obj.check_xml({self.key: xml64})
        invoices = res.get('invoices', {})
        inv_id = invoices.get(self.key, {}).get('invoice_id', False)
        self.assertTrue(inv_id, "Error: Invoice creation")
        # check partner
        inv = self.invoice_obj.browse(inv_id)
        partner = inv.partner_id
        self.assertEqual(partner.vat, self.xml_tree.Emisor.get('Rfc'),
                         "Error: Partner RFC")
        self.assertEqual(partner.name, self.xml_tree.Emisor.get('Nombre'),
                         "Error: Partner Name")
        # Check invoice values
        xml_amount = float(self.xml_tree.get('Total', 0.0))
        self.assertEqual(inv.amount_total, xml_amount, "Error: Total amount")
        xml_tfd = self.invoice_obj.l10n_mx_edi_get_tfd_etree(self.xml_tree)
        uuid = False if xml_tfd is None else xml_tfd.get('UUID', '')
        xml_folio = '%s|%s' % (self.xml_tree.get('Folio', ''), uuid.split(
            '-')[0])
        self.assertEqual(inv.reference, xml_folio, "Error: Reference")

    def test_003_attach_xml_to_invoice_without_uuid(self):
        """Test attach xml in a invoice without uuid and with the same
        reference
        """
        res = self.attach_wizard_obj.check_xml({self.key: base64.b64encode(
            self.xml_str).decode('UTF-8')})
        invoices = res.get('invoices', {})
        inv_id = invoices.get(self.key, {}).get('invoice_id', False)
        inv = self.invoice_obj.browse(inv_id)
        inv.l10n_mx_edi_cfdi_name = False
        inv.reference = inv.reference.split('|')[0]
        res = self.attach_wizard_obj.check_xml({self.key: base64.b64encode(
            self.xml_str).decode('UTF-8')})
        invoices = res.get('invoices', {})
        inv_id = invoices.get(self.key, {}).get('invoice_id', False)
        self.assertEqual(inv_id, inv.id,
                         "Error: attachment generation")
        self.assertTrue(inv.l10n_mx_edi_retrieve_attachments(),
                        "Error: no attachment")

    def test_004_create_invoice_two_times(self):
        """Try to create a invoice two times"""
        res = self.attach_wizard_obj.check_xml({self.key: base64.b64encode(
            self.xml_str).decode('UTF-8')})
        invoices = res.get('invoices', {})
        inv_id = invoices.get(self.key, {}).get('invoice_id', False)
        self.assertTrue(inv_id, "Error: Invoice creation")
        res = self.attach_wizard_obj.check_xml({self.key: base64.b64encode(
            self.xml_str).decode('UTF-8')})
        invoices = res.get('invoices', {})
        inv_id = invoices.get(self.key, {}).get('invoice_id', False)
        self.assertFalse(inv_id,
                         "Error: invoice created in two times")

    def test_005_cfdi_with_fuel_product(self):
        """Verify that consider when the invoice have IEPS"""
        self.env.user.company_id.vat = self.xml_fuel_tree.Receptor.get('Rfc')
        xml64 = base64.b64encode(etree.tostring(
            self.xml_fuel_tree, pretty_print=True, xml_declaration=True,
            encoding='UTF-8')).decode('UTF-8')
        self.attach_wizard_obj.create_partner(xml64, self.key)
        res = self.attach_wizard_obj.check_xml({self.key: xml64})
        invoices = res.get('invoices', {})
        inv_id = invoices.get(self.key, {}).get('invoice_id', False)
        self.assertTrue(inv_id, res)
        # check values
        inv = self.invoice_obj.browse(inv_id)
        xml_amount = float(self.xml_fuel_tree.get('Total', 0.0))
        self.assertEqual(inv.amount_total, xml_amount, "Error: Total amount")
        self.assertEquals(len(inv.invoice_line_ids.ids), 2,
                          'Not created correctly the lines')
        inv.action_invoice_open()
        inv.l10n_mx_edi_update_sat_status()
        self.assertEqual(inv.l10n_mx_edi_sat_status, "valid",
                         inv.message_ids.mapped('body'))
